#!/usr/bin/env bash
# 演示“节点时钟回拨后仍能重读新生效”，并调用确定性的并发重读演示。
#
# 用法：
#   PYTHON=$(which python3) ./scripts/rollback-demo.sh
#   BASE_URL=http://127.0.0.1:8000 ./scripts/rollback-demo.sh   # 复用已运行服务
#
# 前置：pip install -r requirements.txt httpx
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOLUME_ROOT="${CONFIG_VOLUME_ROOT:-${ROOT_DIR}/volumes/config}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"
EARLY_VERSION="..2024-01-01T00_00_00.000000000-Z"

section() { printf '\n========== %s ==========\n' "$*"; }
show() { curl -sS "$@"; printf '\n'; }

mkdir -p "${VOLUME_ROOT}"
rm -rf "${VOLUME_ROOT:?}/"..* "${VOLUME_ROOT}/"*.json 2>/dev/null || true

section "1. 初始化 v1 投射"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" init >/dev/null
LATE_VERSION="$("${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" status \
  | sed -n 's/.*真实版本目录: //p' | xargs basename)"
echo "当前版本: ${LATE_VERSION}"

STARTED_HERE=0
if ! curl -sS -m 2 "${BASE_URL}/health" >/dev/null 2>&1; then
  section "2. 启动服务（关闭后台轮询，单独演示手动重读）"
  CONFIG_VOLUME_ROOT="${VOLUME_ROOT}" \
    "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
    >/tmp/inspection-rollback-demo.log 2>&1 &
  SERVER_PID=$!
  STARTED_HERE=1
  trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT
  for _ in $(seq 1 50); do
    curl -sS -m 1 "${BASE_URL}/health" >/dev/null 2>&1 && break
    sleep 0.2
  done
  echo "服务已启动，pid=${SERVER_PID}"
fi

section "3. 回拨前：v1 目标超时为 3.0"
show "${BASE_URL}/groups/db-inspection"

section "4. 节点时钟回拨：用字典序更早的目录名发布新 v2 投射"
echo "新目录名 ${EARLY_VERSION} < 现行 ${LATE_VERSION} ? \
$("${PYTHON}" -c "print('${EARLY_VERSION}' < '${LATE_VERSION}')")"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" \
  publish-named "${EARLY_VERSION}" v2 | head -1

section "5. 手动重读必须 200，且整版换成新目标（不再按目录名拒绝）"
show -X POST "${BASE_URL}/admin/reload"
show "${BASE_URL}/version"
show "${BASE_URL}/groups/db-inspection"
show "${BASE_URL}/groups/cache-inspection"

section "6. 并发重读交错演示（TestClient 走现有 API：409 / 先生效 / 查询不等待）"
"${PYTHON}" "${ROOT_DIR}/scripts/race_demo.py"

section "回拨演示完成"
