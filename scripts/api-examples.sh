#!/usr/bin/env bash
# 巡检目标配置服务 API 调用示例。
# 用法：./scripts/api-examples.sh [BASE_URL]，默认 http://localhost:8000
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"

echo "== 1. 健康检查（无需配置已生效） =="
curl -sS "${BASE_URL}/health" | python3 -m json.tool

echo
echo "== 2. 查看现行版本号与条目计数 =="
curl -sS "${BASE_URL}/version" | python3 -m json.tool

echo
echo "== 3. 列出全部巡检组 =="
curl -sS "${BASE_URL}/groups" | python3 -m json.tool

echo
echo "== 4. 按组查询：返回现行版本号 + 目标地址/超时（不等重读） =="
curl -sS "${BASE_URL}/groups/db-inspection" | python3 -m json.tool

echo
echo "== 5. 手动触发重读（ConfigMap 换版后调用） =="
curl -sS -X POST "${BASE_URL}/admin/reload" | python3 -m json.tool

echo
echo "== 6. 重读失败的响应形态（4xx + 原因，旧配置继续生效） =="
curl -sS -X POST "${BASE_URL}/admin/reload" | python3 -m json.tool || true

echo
echo "== 7. 交互式 OpenAPI 文档：${BASE_URL}/docs"
