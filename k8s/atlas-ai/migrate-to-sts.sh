#!/usr/bin/env bash
#
# Migrate PostgreSQL and Redis from Deployments to StatefulSets
# in the atlas-ai namespace while preserving PostgreSQL data.
#
# What this script does:
#   1. Scales down backend so nothing is writing to Postgres/Redis
#   2. Scales down the Postgres Deployment
#   3. Patches the PV reclaim policy to Retain (so data survives PVC deletion)
#   4. Deletes the old Deployment and PVC
#   5. Pre-creates the PVC with the name the StatefulSet expects, bound to the same PV
#   6. Applies the new StatefulSet manifests
#   7. Scales backend back up
#
# Redis had no persistence before, so no data migration is needed there.
#
set -euo pipefail

NAMESPACE="atlas-ai"

echo "=== Migration: Deployment -> StatefulSet for Postgres & Redis ==="
echo ""

# ── Step 0: Preflight checks ──────────────────────────────────────
echo "[0/8] Preflight checks..."
kubectl get namespace "$NAMESPACE" > /dev/null 2>&1 || { echo "ERROR: namespace $NAMESPACE not found"; exit 1; }
kubectl get deployment postgres -n "$NAMESPACE" > /dev/null 2>&1 || { echo "ERROR: postgres Deployment not found — already migrated?"; exit 1; }

# ── Step 1: Scale down backend to stop DB connections ─────────────
echo "[1/8] Scaling down backend..."
kubectl scale statefulset backend -n "$NAMESPACE" --replicas=0 --timeout=60s 2>/dev/null || true
kubectl rollout status statefulset backend -n "$NAMESPACE" --timeout=120s 2>/dev/null || true
echo "      Backend scaled down."

# ── Step 2: Scale down Postgres Deployment ────────────────────────
echo "[2/8] Scaling down postgres Deployment..."
kubectl scale deployment postgres -n "$NAMESPACE" --replicas=0 --timeout=60s
kubectl rollout status deployment postgres -n "$NAMESPACE" --timeout=120s
echo "      Postgres Deployment scaled down."

# ── Step 3: Get PV name and set Retain policy ────────────────────
echo "[3/8] Setting PV reclaim policy to Retain..."
PV_NAME=$(kubectl get pvc postgres-pvc -n "$NAMESPACE" -o jsonpath='{.spec.volumeName}')
if [ -z "$PV_NAME" ]; then
    echo "ERROR: Could not find PV bound to postgres-pvc"
    exit 1
fi
echo "      PV: $PV_NAME"
kubectl patch pv "$PV_NAME" -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
echo "      Reclaim policy set to Retain."

# ── Step 4: Delete old Postgres Deployment and PVC ────────────────
echo "[4/8] Deleting old postgres Deployment..."
kubectl delete deployment postgres -n "$NAMESPACE" --timeout=60s
echo "      Deployment deleted."

echo "[5/8] Deleting old PVC (PV is retained)..."
kubectl delete pvc postgres-pvc -n "$NAMESPACE" --timeout=60s
echo "      PVC deleted."

# ── Step 5: Remove claimRef from PV so it can be rebound ─────────
echo "[6/8] Removing claimRef from PV to make it Available..."
kubectl patch pv "$PV_NAME" --type=json -p='[{"op":"remove","path":"/spec/claimRef"}]'
echo "      PV is now Available."

# ── Step 6: Delete old Redis Deployment ───────────────────────────
echo "[7/8] Deleting old redis Deployment..."
kubectl delete deployment redis -n "$NAMESPACE" --timeout=60s 2>/dev/null || echo "      (redis Deployment not found, skipping)"

# ── Step 7: Pre-create PVC for postgres StatefulSet ───────────────
echo "[7/8] Pre-creating PVC for StatefulSet (postgres-data-postgres-0)..."
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: postgres-data-postgres-0
  namespace: ${NAMESPACE}
  labels:
    app: postgres
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  volumeName: ${PV_NAME}
EOF
echo "      PVC postgres-data-postgres-0 created and bound to $PV_NAME."

# Wait for PVC to bind
echo "      Waiting for PVC to bind..."
kubectl wait --for=jsonpath='{.status.phase}'=Bound pvc/postgres-data-postgres-0 -n "$NAMESPACE" --timeout=30s

# ── Step 8: Apply new StatefulSet manifests ───────────────────────
echo "[8/8] Applying StatefulSet manifests..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kubectl apply -f "$SCRIPT_DIR/redis.yaml"
kubectl apply -f "$SCRIPT_DIR/postgres.yaml"
echo "      StatefulSets applied."

# ── Step 9: Wait for pods to be ready ─────────────────────────────
echo ""
echo "Waiting for postgres-0 to be ready..."
kubectl rollout status statefulset postgres -n "$NAMESPACE" --timeout=180s

echo "Waiting for redis-0 to be ready..."
kubectl rollout status statefulset redis -n "$NAMESPACE" --timeout=120s

# ── Step 10: Scale backend back up ────────────────────────────────
echo ""
echo "Scaling backend back up..."
kubectl scale statefulset backend -n "$NAMESPACE" --replicas=1
kubectl rollout status statefulset backend -n "$NAMESPACE" --timeout=180s

echo ""
echo "=== Migration complete! ==="
echo ""
echo "Verify:"
echo "  kubectl get statefulset -n $NAMESPACE"
echo "  kubectl get pvc -n $NAMESPACE"
echo "  kubectl get pods -n $NAMESPACE"
echo ""
echo "If everything looks good, you can delete the old PV Retain policy:"
echo "  kubectl patch pv $PV_NAME -p '{\"spec\":{\"persistentVolumeReclaimPolicy\":\"Delete\"}}'"
