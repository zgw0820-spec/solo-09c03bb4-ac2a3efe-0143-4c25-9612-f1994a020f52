#!/usr/bin/env bash
# 端到端演练：模拟 K8s ConfigMap 投射卷换版，验证不重启进程的重读流程。
#
# 用法：
#   ./scripts/e2e-demo.sh                 # 用 ./volumes/config 作为模拟卷
#   CONFIG_VOLUME_ROOT=/tmp/x ./scripts/e2e-demo.sh
#
# 脚本会：
#   1) 初始化 v1 投射并启动 uvicorn（或复用已在运行的服务）
#   2) 演练正常换版 v1 -> v2
#   3) 演练引用缺失的坏投射（重读失败、旧配置保留）
#   4) 演练 prune 旧版本目录后服务不受影响
#   5) 演练两版文件混用（服务拒绝，旧配置保留）
#   6) 恢复正常投射并确认服务恢复，最后关闭演练进程
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VOLUME_ROOT="${CONFIG_VOLUME_ROOT:-${ROOT_DIR}/volumes/config}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
PYTHON="${PYTHON:-python3}"

section() { printf '\n========== %s ==========\n' "$*"; }
show() { curl -sS "$@"; printf '\n'; }

mkdir -p "${VOLUME_ROOT}"
rm -rf "${VOLUME_ROOT:?}/"..* "${VOLUME_ROOT}/"*.json 2>/dev/null || true

section "1. 初始化 v1 投射（K8s 风格 ..data 符号链接）"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" init

if ! curl -sS -m 2 "${BASE_URL}/health" >/dev/null 2>&1; then
  section "2. 启动服务（单实例；容器场景等价于 docker compose up -d）"
  CONFIG_VOLUME_ROOT="${VOLUME_ROOT}" \
    "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 \
    >/tmp/inspection-config-demo.log 2>&1 &
  SERVER_PID=$!
  trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT
  until curl -sS -m 1 "${BASE_URL}/health" >/dev/null 2>&1; do sleep 0.3; done
  echo "服务已启动，pid=${SERVER_PID}"
else
  echo "检测到服务已在运行，直接复用: ${BASE_URL}"
fi

section "3. 现行版本与按组查询（v1）"
show "${BASE_URL}/health"
show "${BASE_URL}/version"
show "${BASE_URL}/groups/db-inspection"

section "4. 原子换版到 v2，再调用重读接口（不重启进程）"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" publish v2
show -X POST "${BASE_URL}/admin/reload"
show "${BASE_URL}/groups/db-inspection"
show "${BASE_URL}/groups/cache-inspection"

section "5. 换入引用缺失的坏投射：重读必须失败，旧配置继续生效"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" bad-swap
show -X POST "${BASE_URL}/admin/reload" || true
echo "-- 现行版本应仍为 v2 的时间戳版本："
show "${BASE_URL}/version"

section "6. prune 旧版本目录（模拟 kubelet 清理），当前版本与查询不受影响"
# 先恢复一个好投射再清理，保证被保留的是有效版本。
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" publish v2
show -X POST "${BASE_URL}/admin/reload"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" prune
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" status
show "${BASE_URL}/groups/network-inspection"

section "7. 制造两版文件混用：..data 指新版、groups 链接留在旧版，必须拒绝"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" mixed-swap
show -X POST "${BASE_URL}/admin/reload" || true
echo "-- 现行版本保持不变："
show "${BASE_URL}/version"

section "8. 恢复正常投射，服务恢复"
"${PYTHON}" "${ROOT_DIR}/scripts/kube_sim.py" --root "${VOLUME_ROOT}" publish v2
show -X POST "${BASE_URL}/admin/reload"
show "${BASE_URL}/version"

section "演练完成"
