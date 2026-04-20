"""Kubernetes pod spawner for per-task worker pods.

Creates a dedicated pod + PVC for each task execution. The pod runs the
worker entrypoint (agents.worker) which handles a single task's lifecycle.

Backend-0 acts as the control plane — spawning, monitoring, and cleaning
up task pods via the K8s API.

NOTE: The kubernetes Python client is synchronous. All K8s API calls are
wrapped with asyncio.to_thread() to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# K8s client is imported lazily to avoid hard dependency in dev/test
_k8s_loaded = False
_core_v1 = None


def _ensure_k8s():
    """Lazy-load kubernetes client and configure from in-cluster or kubeconfig."""
    global _k8s_loaded, _core_v1
    if _k8s_loaded:
        return
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    _core_v1 = client.CoreV1Api()
    _k8s_loaded = True


def _get_core_v1():
    _ensure_k8s()
    return _core_v1


# ── Configuration ────────────────────────────────────────────────────

# Read from env (set on backend-0 deployment)
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "atlas-ai")
K8S_DEFAULT_IMAGE = os.environ.get("K8S_WORKER_IMAGE", "")  # falls back to own image
K8S_SERVICE_ACCOUNT = os.environ.get("K8S_WORKER_SERVICE_ACCOUNT", "default")
K8S_STORAGE_CLASS = os.environ.get("K8S_STORAGE_CLASS", "")  # empty = cluster default
K8S_NODE_SELECTOR = os.environ.get("K8S_NODE_SELECTOR", "")  # e.g. "pool=workers"
K8S_WORKER_PORT = 8000


def _default_image() -> str:
    """Resolve default worker image — same as backend-0's own image."""
    if K8S_DEFAULT_IMAGE:
        return K8S_DEFAULT_IMAGE
    # Fallback: try to read from own pod spec (K8s downward API or env)
    return os.environ.get("BACKEND_IMAGE", "agents-backend:latest")


def _pod_name(todo_id: str) -> str:
    """Generate deterministic pod name from todo_id.

    Uses first 12 chars of UUID (without hyphens) for uniqueness.
    K8s names must be lowercase DNS-compatible, max 63 chars.
    """
    short = todo_id.replace("-", "")[:12]
    return f"task-worker-{short}"


def _pvc_name(todo_id: str) -> str:
    """Generate deterministic PVC name from todo_id."""
    short = todo_id.replace("-", "")[:12]
    return f"task-pvc-{short}"


# ── PVC Creation ─────────────────────────────────────────────────────


async def create_pvc(
    todo_id: str,
    size_gb: int = 20,
    namespace: str | None = None,
) -> str:
    """Create a PersistentVolumeClaim for the task workspace."""
    from kubernetes import client

    _ensure_k8s()
    ns = namespace or K8S_NAMESPACE
    name = _pvc_name(todo_id)

    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=ns,
            labels={
                "app": "task-worker",
                "todo-id": todo_id[:8],
                "managed-by": "agents-orchestrator",
            },
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": f"{size_gb}Gi"},
            ),
            storage_class_name=K8S_STORAGE_CLASS or None,
        ),
    )

    def _do_create():
        try:
            _core_v1.create_namespaced_persistent_volume_claim(namespace=ns, body=pvc)
            logger.info("Created PVC %s (%dGi) in %s", name, size_gb, ns)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                logger.info("PVC %s already exists", name)
            else:
                raise

    await asyncio.to_thread(_do_create)
    return name


# ── Pod Creation ─────────────────────────────────────────────────────


async def create_task_pod(
    todo_id: str,
    project_id: str,
    *,
    image: str | None = None,
    pvc_name: str | None = None,
    boot_script: str | None = None,
    namespace: str | None = None,
    env_overrides: dict[str, str] | None = None,
    resources: dict[str, str] | None = None,
    node_type: str | None = None,
    pod_spec_override: dict | None = None,
) -> str:
    """Create a dedicated pod for a task.

    The pod runs `python -m agents.worker --todo-id <id>` which:
    1. Optionally executes the boot_script
    2. Connects to shared Postgres + Redis
    3. Runs the TaskScheduler for the single task
    4. Exits when task reaches terminal state
    """
    from kubernetes import client

    _ensure_k8s()
    ns = namespace or K8S_NAMESPACE
    pod_name = _pod_name(todo_id)
    pvc = pvc_name or _pvc_name(todo_id)
    img = image or _default_image()

    # Build environment variables — inherit critical ones from backend-0
    env_vars = [
        client.V1EnvVar(name="DATABASE_URL", value=os.environ.get("DATABASE_URL", "")),
        client.V1EnvVar(name="REDIS_URL", value=os.environ.get("REDIS_URL", "redis://redis:6379/0")),
        client.V1EnvVar(name="WORKSPACE_ROOT", value="/data/workspace"),
        client.V1EnvVar(name="TASK_POD_MODE", value="true"),
        client.V1EnvVar(name="TASK_TODO_ID", value=todo_id),
        client.V1EnvVar(name="TASK_PROJECT_ID", value=project_id),
        client.V1EnvVar(name="ENCRYPTION_KEY", value=os.environ.get("ENCRYPTION_KEY", "")),
        client.V1EnvVar(name="JWT_SECRET_KEY", value=os.environ.get("JWT_SECRET_KEY", "")),
        client.V1EnvVar(name="LOG_LEVEL", value=os.environ.get("LOG_LEVEL", "INFO")),
        # Pod identity via downward API
        client.V1EnvVar(
            name="POD_IP",
            value_from=client.V1EnvVarSource(
                field_ref=client.V1ObjectFieldSelector(field_path="status.podIP"),
            ),
        ),
    ]

    # Pass boot script as env var (will be executed by worker entrypoint)
    if boot_script:
        env_vars.append(client.V1EnvVar(name="BOOT_SCRIPT", value=boot_script))

    # Inherit provider API keys
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(key)
        if val:
            env_vars.append(client.V1EnvVar(name=key, value=val))

    # Custom env overrides
    if env_overrides:
        for k, v in env_overrides.items():
            env_vars.append(client.V1EnvVar(name=k, value=v))

    # Volume mount
    volume_mount = client.V1VolumeMount(
        name="workspace",
        mount_path="/data/workspace",
    )

    # Resource limits — configurable via project settings
    res = resources or {}
    cpu_request = res.get("cpu_request", "500m")
    mem_request = res.get("memory_request", "1Gi")
    cpu_limit = res.get("cpu_limit", "4")
    mem_limit = res.get("memory_limit", "8Gi")

    # Container definition
    container = client.V1Container(
        name="worker",
        image=img,
        image_pull_policy="IfNotPresent",
        command=["python", "-m", "agents.worker"],
        args=["--todo-id", todo_id],
        env=env_vars,
        volume_mounts=[volume_mount],
        ports=[client.V1ContainerPort(container_port=K8S_WORKER_PORT, name="http")],
        resources=client.V1ResourceRequirements(
            requests={"cpu": cpu_request, "memory": mem_request},
            limits={"cpu": cpu_limit, "memory": mem_limit},
        ),
        liveness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path="/healthz", port=K8S_WORKER_PORT),
            initial_delay_seconds=30,
            period_seconds=30,
            failure_threshold=3,
        ),
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path="/healthz", port=K8S_WORKER_PORT),
            initial_delay_seconds=5,
            period_seconds=10,
        ),
    )

    # ── Advanced mode: raw pod spec override ────────────────────────
    # When pod_spec_override is provided, it's used as the full pod spec dict.
    # We still inject required fields (name, labels, PVC volume, env vars) to
    # ensure the worker can function, but the user controls everything else.
    if pod_spec_override:
        pod_dict = _build_override_pod(
            pod_spec_override,
            pod_name=pod_name,
            namespace=ns,
            todo_id=todo_id,
            project_id=project_id,
            pvc=pvc,
            img=img,
            env_vars=env_vars,
            boot_script=boot_script,
        )

        def _do_create_raw():
            try:
                _core_v1.create_namespaced_pod(namespace=ns, body=pod_dict)
                logger.info("Created task pod %s (advanced spec) in %s", pod_name, ns)
            except client.exceptions.ApiException as e:
                if e.status == 409:
                    logger.info("Pod %s already exists, checking state", pod_name)
                else:
                    raise

        await asyncio.to_thread(_do_create_raw)
        return pod_name

    # ── Standard mode: build pod spec from settings ───────────────
    # Node selector from env (global default)
    node_selector = None
    if K8S_NODE_SELECTOR:
        node_selector = dict(
            pair.split("=", 1) for pair in K8S_NODE_SELECTOR.split(",") if "=" in pair
        )

    # Node affinity for node_type — matches label "nodeType: <value>"
    affinity = None
    if node_type:
        affinity = client.V1Affinity(
            node_affinity=client.V1NodeAffinity(
                required_during_scheduling_ignored_during_execution=client.V1NodeSelector(
                    node_selector_terms=[
                        client.V1NodeSelectorTerm(
                            match_expressions=[
                                client.V1NodeSelectorRequirement(
                                    key="nodeType",
                                    operator="In",
                                    values=[node_type],
                                ),
                            ],
                        ),
                    ],
                ),
            ),
        )

    # Pod spec
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            namespace=ns,
            labels={
                "app": "task-worker",
                "todo-id": todo_id[:8],
                "project-id": project_id[:8],
                "managed-by": "agents-orchestrator",
            },
        ),
        spec=client.V1PodSpec(
            restart_policy="OnFailure",
            service_account_name=K8S_SERVICE_ACCOUNT,
            node_selector=node_selector,
            affinity=affinity,
            containers=[container],
            volumes=[
                client.V1Volume(
                    name="workspace",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=pvc,
                    ),
                ),
            ],
        ),
    )

    def _do_create():
        try:
            _core_v1.create_namespaced_pod(namespace=ns, body=pod)
            logger.info("Created task pod %s (image=%s) in %s", pod_name, img, ns)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                logger.info("Pod %s already exists, checking state", pod_name)
            else:
                raise

    await asyncio.to_thread(_do_create)
    return pod_name


def _build_override_pod(
    spec_override: dict,
    *,
    pod_name: str,
    namespace: str,
    todo_id: str,
    project_id: str,
    pvc: str,
    img: str,
    env_vars: list,
    boot_script: str | None,
) -> dict:
    """Build a pod dict from user-provided spec override.

    Ensures required fields are injected so the worker can function:
    - metadata.name, namespace, labels
    - The workspace PVC volume + mount
    - Required env vars (merged with any user-defined ones)
    - Command/args pointing to the worker entrypoint
    """
    import copy

    pod_dict = copy.deepcopy(spec_override)

    # Ensure top-level structure
    pod_dict.setdefault("apiVersion", "v1")
    pod_dict.setdefault("kind", "Pod")
    pod_dict.setdefault("metadata", {})
    pod_dict["metadata"]["name"] = pod_name
    pod_dict["metadata"]["namespace"] = namespace
    pod_dict["metadata"].setdefault("labels", {})
    pod_dict["metadata"]["labels"].update({
        "app": "task-worker",
        "todo-id": todo_id[:8],
        "project-id": project_id[:8],
        "managed-by": "agents-orchestrator",
    })

    # Ensure spec
    pod_dict.setdefault("spec", {})
    spec = pod_dict["spec"]
    spec.setdefault("restartPolicy", "OnFailure")

    # Ensure workspace volume exists
    spec.setdefault("volumes", [])
    has_ws_vol = any(v.get("name") == "workspace" for v in spec["volumes"])
    if not has_ws_vol:
        spec["volumes"].append({
            "name": "workspace",
            "persistentVolumeClaim": {"claimName": pvc},
        })

    # Ensure at least one container with the worker entrypoint
    spec.setdefault("containers", [{}])
    main_container = spec["containers"][0]
    main_container.setdefault("name", "worker")
    main_container.setdefault("image", img)
    main_container["command"] = ["python", "-m", "agents.worker"]
    main_container["args"] = ["--todo-id", todo_id]

    # Merge env vars (user's take precedence for same name)
    existing_env = {e["name"]: e for e in main_container.get("env", [])}
    from kubernetes import client as _client
    for ev in env_vars:
        if isinstance(ev, _client.V1EnvVar):
            env_dict: dict = {"name": ev.name}
            if ev.value is not None:
                env_dict["value"] = ev.value
            elif ev.value_from:
                env_dict["valueFrom"] = {"fieldRef": {"fieldPath": ev.value_from.field_ref.field_path}}
            existing_env.setdefault(ev.name, env_dict)
        else:
            existing_env.setdefault(ev.get("name", ""), ev)
    main_container["env"] = list(existing_env.values())

    # Ensure workspace volume mount
    main_container.setdefault("volumeMounts", [])
    has_ws_mount = any(m.get("name") == "workspace" for m in main_container["volumeMounts"])
    if not has_ws_mount:
        main_container["volumeMounts"].append({
            "name": "workspace",
            "mountPath": "/data/workspace",
        })

    # Ensure port
    main_container.setdefault("ports", [{"containerPort": K8S_WORKER_PORT, "name": "http"}])

    return pod_dict


# ── Pod Status ───────────────────────────────────────────────────────


async def get_pod_status(pod_name: str, namespace: str | None = None) -> dict[str, Any]:
    """Get pod status including IP and phase."""
    from kubernetes import client

    _ensure_k8s()
    ns = namespace or K8S_NAMESPACE

    def _do_get():
        try:
            pod = _core_v1.read_namespaced_pod(name=pod_name, namespace=ns)
            return {
                "phase": pod.status.phase,  # Pending, Running, Succeeded, Failed
                "pod_ip": pod.status.pod_ip,
                "started_at": (
                    pod.status.start_time.isoformat() if pod.status.start_time else None
                ),
                "conditions": [
                    {"type": c.type, "status": c.status}
                    for c in (pod.status.conditions or [])
                ],
            }
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return {"phase": "NotFound", "pod_ip": None}
            raise

    return await asyncio.to_thread(_do_get)


# ── Cleanup ──────────────────────────────────────────────────────────


async def delete_task_pod(todo_id: str, namespace: str | None = None) -> None:
    """Delete the task pod (graceful termination)."""
    from kubernetes import client

    _ensure_k8s()
    ns = namespace or K8S_NAMESPACE
    name = _pod_name(todo_id)

    def _do_delete():
        try:
            _core_v1.delete_namespaced_pod(
                name=name,
                namespace=ns,
                body=client.V1DeleteOptions(grace_period_seconds=30),
            )
            logger.info("Deleted task pod %s", name)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.debug("Pod %s already deleted", name)
            else:
                raise

    await asyncio.to_thread(_do_delete)


async def delete_task_pvc(todo_id: str, namespace: str | None = None) -> None:
    """Delete the task PVC (workspace data is lost)."""
    from kubernetes import client

    _ensure_k8s()
    ns = namespace or K8S_NAMESPACE
    name = _pvc_name(todo_id)

    def _do_delete():
        try:
            _core_v1.delete_namespaced_persistent_volume_claim(name=name, namespace=ns)
            logger.info("Deleted task PVC %s", name)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.debug("PVC %s already deleted", name)
            else:
                raise

    await asyncio.to_thread(_do_delete)


async def cleanup_task_resources(todo_id: str, namespace: str | None = None) -> None:
    """Delete both pod and PVC for a task."""
    await delete_task_pod(todo_id, namespace)
    await delete_task_pvc(todo_id, namespace)


# ── DB helpers ───────────────────────────────────────────────────────


async def record_pod_created(
    db: asyncpg.Pool,
    todo_id: str,
    project_id: str,
    pod_name: str,
    pvc_name: str,
    image: str,
    pvc_size_gb: int,
    boot_script: str | None = None,
    namespace: str | None = None,
) -> str:
    """Insert task_pods record. Returns the row id."""
    ns = namespace or K8S_NAMESPACE
    row = await db.fetchrow(
        """
        INSERT INTO task_pods (
            todo_id, project_id, pod_name, pvc_name, namespace,
            image, pvc_size_gb, boot_script, state
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'creating')
        ON CONFLICT (todo_id) DO UPDATE SET
            pod_name = EXCLUDED.pod_name,
            pvc_name = EXCLUDED.pvc_name,
            image = EXCLUDED.image,
            pvc_size_gb = EXCLUDED.pvc_size_gb,
            boot_script = EXCLUDED.boot_script,
            state = 'creating',
            error_message = NULL,
            created_at = NOW()
        RETURNING id
        """,
        todo_id, project_id, pod_name, pvc_name, ns,
        image, pvc_size_gb, boot_script,
    )
    return str(row["id"])


async def update_pod_state(
    db: asyncpg.Pool,
    todo_id: str,
    state: str,
    *,
    pod_ip: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update task_pods state and optional fields."""
    sets = ["state = $2", "stopped_at = CASE WHEN $2 IN ('terminated', 'failed') THEN NOW() ELSE stopped_at END"]
    params: list = [todo_id, state]

    if pod_ip is not None:
        sets.append(f"pod_ip = ${len(params) + 1}")
        params.append(pod_ip)
    if error_message is not None:
        sets.append(f"error_message = ${len(params) + 1}")
        params.append(error_message)
    if state == "running":
        sets.append("started_at = NOW()")

    query = f"UPDATE task_pods SET {', '.join(sets)} WHERE todo_id = $1"
    await db.execute(query, *params)


async def get_pod_record(db: asyncpg.Pool, todo_id: str) -> dict | None:
    """Get task_pods record for a todo."""
    row = await db.fetchrow(
        "SELECT * FROM task_pods WHERE todo_id = $1", todo_id
    )
    return dict(row) if row else None
