"""Kubernetes pod spawner for per-task worker pods.

Creates a dedicated pod + PVC for each task execution. The pod runs the
worker entrypoint (agents.worker) which handles a single task's lifecycle.

Uses kubectl CLI instead of the kubernetes Python client — simpler auth
handling (inherits kubeconfig/in-cluster config the same way kubectl does),
easier to debug (same commands work from exec), no Python k8s dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────

K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "atlas-ai")
K8S_DEFAULT_IMAGE = os.environ.get("K8S_WORKER_IMAGE", "")
K8S_SERVICE_ACCOUNT = os.environ.get("K8S_WORKER_SERVICE_ACCOUNT", "default")
K8S_STORAGE_CLASS = os.environ.get("K8S_STORAGE_CLASS", "")
K8S_NODE_SELECTOR = os.environ.get("K8S_NODE_SELECTOR", "")
K8S_WORKER_PORT = 8000


def _backend_image() -> str:
    """The backend image — always the agents-platform image."""
    return os.environ.get("BACKEND_IMAGE", K8S_DEFAULT_IMAGE or "agents-backend:latest")


def _pod_name(todo_id: str) -> str:
    short = todo_id.replace("-", "")[:12]
    return f"task-worker-{short}"


def _pvc_name(todo_id: str) -> str:
    short = todo_id.replace("-", "")[:12]
    return f"task-pvc-{short}"


# ── kubectl runner ───────────────────────────────────────────────────

async def _kubectl(*args: str, input_data: str | None = None, timeout: int = 30) -> tuple[int, str, str]:
    """Run a kubectl command and return (exit_code, stdout, stderr)."""
    cmd = ["kubectl", *args]
    logger.debug("kubectl: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_data else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=input_data.encode() if input_data else None),
        timeout=timeout,
    )
    return proc.returncode, stdout.decode(), stderr.decode()


async def _kubectl_apply(manifest: dict, namespace: str | None = None) -> None:
    """Apply a K8s manifest dict via kubectl apply."""
    ns = namespace or K8S_NAMESPACE
    manifest_json = json.dumps(manifest)
    rc, out, err = await _kubectl("apply", "-n", ns, "-f", "-", input_data=manifest_json)
    if rc != 0:
        raise RuntimeError(f"kubectl apply failed (exit {rc}): {err.strip()}")
    logger.info("kubectl apply: %s", out.strip())


# ── PVC Creation ─────────────────────────────────────────────────────

async def create_pvc(
    todo_id: str,
    size_gb: int = 20,
    namespace: str | None = None,
) -> str:
    """Create a PersistentVolumeClaim for the task workspace."""
    ns = namespace or K8S_NAMESPACE
    name = _pvc_name(todo_id)

    manifest: dict = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {
                "app": "task-worker",
                "todo-id": todo_id[:8],
                "managed-by": "agents-orchestrator",
            },
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{size_gb}Gi"}},
        },
    }
    if K8S_STORAGE_CLASS:
        manifest["spec"]["storageClassName"] = K8S_STORAGE_CLASS

    await _kubectl_apply(manifest, ns)
    logger.info("Created PVC %s (%dGi) in %s", name, size_gb, ns)
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

    Two modes based on whether a custom project image is provided:

    1. **No custom image** (image is backend image or not set):
       Single container with the backend image. Has agents code + Python,
       but no project-specific toolchain. boot_script can install deps.

    2. **Custom project image** (image differs from backend image):
       Init container pattern. The backend image copies the agents Python
       packages to a shared volume. The main container uses the custom image
       (which has the project's toolchain: node/cargo/go + deps) and picks
       up the agents code via PYTHONPATH. The custom image only needs Python 3.12+.
    """
    ns = namespace or K8S_NAMESPACE
    name = _pod_name(todo_id)
    pvc = pvc_name or _pvc_name(todo_id)
    backend_img = _backend_image()
    project_img = image or ""
    # Determine if we need the init container pattern
    use_init_container = bool(project_img) and project_img != backend_img

    worker_img = project_img if use_init_container else backend_img

    # Build env vars
    env = [
        {"name": "DATABASE_URL", "value": os.environ.get("DATABASE_URL", "")},
        {"name": "REDIS_URL", "value": os.environ.get("REDIS_URL", "redis://redis:6379/0")},
        {"name": "WORKSPACE_ROOT", "value": "/data/workspace"},
        {"name": "TASK_POD_MODE", "value": "true"},
        {"name": "TASK_TODO_ID", "value": todo_id},
        {"name": "TASK_PROJECT_ID", "value": project_id},
        {"name": "ENCRYPTION_KEY", "value": os.environ.get("ENCRYPTION_KEY", "")},
        {"name": "JWT_SECRET_KEY", "value": os.environ.get("JWT_SECRET_KEY", "")},
        {"name": "LOG_LEVEL", "value": os.environ.get("LOG_LEVEL", "INFO")},
        {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
    ]
    if boot_script:
        env.append({"name": "BOOT_SCRIPT", "value": boot_script})
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(key)
        if val:
            env.append({"name": key, "value": val})
    if env_overrides:
        for k, v in env_overrides.items():
            env.append({"name": k, "value": v})

    # When using init container, agents code lives at /agent-runtime
    if use_init_container:
        env.append({"name": "PYTHONPATH", "value": "/agent-runtime/site-packages"})

    # Resource limits
    res = resources or {}
    resource_spec = {
        "requests": {
            "cpu": res.get("cpu_request", "500m"),
            "memory": res.get("memory_request", "1Gi"),
        },
        "limits": {
            "cpu": res.get("cpu_limit", "4"),
            "memory": res.get("memory_limit", "8Gi"),
        },
    }

    volume_mounts = [{"name": "workspace", "mountPath": "/data/workspace"}]
    if use_init_container:
        volume_mounts.append({"name": "agent-runtime", "mountPath": "/agent-runtime"})

    container = {
        "name": "worker",
        "image": worker_img,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "agents.worker"],
        "args": ["--todo-id", todo_id],
        "env": env,
        "volumeMounts": volume_mounts,
        "ports": [{"containerPort": K8S_WORKER_PORT, "name": "http"}],
        "resources": resource_spec,
        "livenessProbe": {
            "httpGet": {"path": "/healthz", "port": K8S_WORKER_PORT},
            "initialDelaySeconds": 30,
            "periodSeconds": 30,
            "failureThreshold": 3,
        },
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": K8S_WORKER_PORT},
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
        },
    }

    # Build pod spec
    if pod_spec_override:
        manifest = _build_override_pod(
            pod_spec_override,
            pod_name=name, namespace=ns,
            todo_id=todo_id, project_id=project_id,
            pvc=pvc, img=worker_img, env_vars=env, boot_script=boot_script,
        )
    else:
        volumes: list[dict] = [
            {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}},
        ]
        if use_init_container:
            volumes.append({"name": "agent-runtime", "emptyDir": {}})

        spec: dict[str, Any] = {
            "restartPolicy": "OnFailure",
            "serviceAccountName": K8S_SERVICE_ACCOUNT,
            "containers": [container],
            "volumes": volumes,
        }

        # Init container: copies agents Python packages from backend image
        # to a shared emptyDir volume. The main container (custom project image)
        # picks them up via PYTHONPATH=/agent-runtime/site-packages.
        if use_init_container:
            spec["initContainers"] = [{
                "name": "agent-runtime-init",
                "image": backend_img,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c",
                    "cp -r /usr/local/lib/python3.12/site-packages /agent-runtime/site-packages "
                    "&& cp -r /app/src /agent-runtime/site-packages/"
                ],
                "volumeMounts": [
                    {"name": "agent-runtime", "mountPath": "/agent-runtime"},
                ],
            }]
            logger.info(
                "Using init container pattern: backend=%s project=%s",
                backend_img, project_img,
            )

        if K8S_NODE_SELECTOR:
            spec["nodeSelector"] = dict(
                pair.split("=", 1) for pair in K8S_NODE_SELECTOR.split(",") if "=" in pair
            )
        if node_type:
            spec["affinity"] = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{
                            "matchExpressions": [{
                                "key": "nodeType", "operator": "In", "values": [node_type],
                            }],
                        }],
                    },
                },
            }

        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": ns,
                "labels": {
                    "app": "task-worker",
                    "todo-id": todo_id[:8],
                    "project-id": project_id[:8],
                    "managed-by": "agents-orchestrator",
                },
            },
            "spec": spec,
        }

    await _kubectl_apply(manifest, ns)
    logger.info("Created task pod %s (image=%s, init=%s) in %s", name, worker_img, use_init_container, ns)
    return name


def _build_override_pod(
    spec_override: dict, *,
    pod_name: str, namespace: str, todo_id: str, project_id: str,
    pvc: str, img: str, env_vars: list[dict], boot_script: str | None,
) -> dict:
    """Build a pod dict from user-provided spec override."""
    import copy
    pod_dict = copy.deepcopy(spec_override)

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

    pod_dict.setdefault("spec", {})
    spec = pod_dict["spec"]
    spec.setdefault("restartPolicy", "OnFailure")

    spec.setdefault("volumes", [])
    if not any(v.get("name") == "workspace" for v in spec["volumes"]):
        spec["volumes"].append({"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}})

    spec.setdefault("containers", [{}])
    c = spec["containers"][0]
    c.setdefault("name", "worker")
    c.setdefault("image", img)
    c["command"] = ["python", "-m", "agents.worker"]
    c["args"] = ["--todo-id", todo_id]

    existing_env = {e["name"]: e for e in c.get("env", [])}
    for ev in env_vars:
        existing_env.setdefault(ev["name"], ev)
    c["env"] = list(existing_env.values())

    c.setdefault("volumeMounts", [])
    if not any(m.get("name") == "workspace" for m in c["volumeMounts"]):
        c["volumeMounts"].append({"name": "workspace", "mountPath": "/data/workspace"})

    c.setdefault("ports", [{"containerPort": K8S_WORKER_PORT, "name": "http"}])

    return pod_dict


# ── Pod Status ───────────────────────────────────────────────────────

async def get_pod_status(pod_name: str, namespace: str | None = None) -> dict[str, Any]:
    """Get pod status via kubectl."""
    ns = namespace or K8S_NAMESPACE
    rc, out, err = await _kubectl(
        "get", "pod", pod_name, "-n", ns,
        "-o", "jsonpath={.status.phase},{.status.podIP},{.status.startTime}",
    )
    if rc != 0:
        if "NotFound" in err or "not found" in err.lower():
            return {"phase": "NotFound", "pod_ip": None}
        raise RuntimeError(f"kubectl get pod failed: {err.strip()}")

    parts = out.split(",", 2)
    return {
        "phase": parts[0] if parts[0] else "Unknown",
        "pod_ip": parts[1] if len(parts) > 1 and parts[1] else None,
        "started_at": parts[2] if len(parts) > 2 and parts[2] else None,
    }


# ── Cleanup ──────────────────────────────────────────────────────────

async def delete_task_pod(todo_id: str, namespace: str | None = None) -> None:
    ns = namespace or K8S_NAMESPACE
    name = _pod_name(todo_id)
    rc, out, err = await _kubectl(
        "delete", "pod", name, "-n", ns,
        "--grace-period=30", "--ignore-not-found=true",
    )
    if rc == 0:
        logger.info("Deleted task pod %s", name)
    else:
        logger.warning("Failed to delete pod %s: %s", name, err.strip())


async def delete_task_pvc(todo_id: str, namespace: str | None = None) -> None:
    ns = namespace or K8S_NAMESPACE
    name = _pvc_name(todo_id)
    rc, out, err = await _kubectl(
        "delete", "pvc", name, "-n", ns, "--ignore-not-found=true",
    )
    if rc == 0:
        logger.info("Deleted task PVC %s", name)
    else:
        logger.warning("Failed to delete PVC %s: %s", name, err.strip())


async def cleanup_task_resources(todo_id: str, namespace: str | None = None) -> None:
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
    row = await db.fetchrow(
        "SELECT * FROM task_pods WHERE todo_id = $1", todo_id
    )
    return dict(row) if row else None
