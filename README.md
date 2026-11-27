# 巡检目标配置服务（免重启重读 ConfigMap）

备份巡检任务切换目标地址/超时时**不重启 Pod**：FastAPI 提供重读接口，
从同一个 Kubernetes ConfigMap 投射卷读取两份 JSON，校验通过后原子生效。

## 配置文件

两份 JSON 同处一个 ConfigMap、同一次投射到挂载根目录（默认
`/etc/inspection-config`）：

| 文件 | 内容 |
| --- | --- |
| `inspection-groups.json` | 各巡检组及其引用的目标 id 列表（`groups[].target_ids`） |
| `targets.json` | 目标 id -> 地址（`address`）与超时（`timeout_seconds`，秒，> 0） |

示例见 `manifests/configmap.yaml`。

## 一致性保证（对应需求逐条）

1. **同一次投射才一起生效**：挂载根下的 `..data` 是 kubelet 原子
   rename 的符号链接，服务先 `realpath(..data)` 定位当前真实版本目录，
   两份文件都只从该目录打开；同时校验根目录文件链接与 `..data` 指向
   同一版本，**两版文件混用直接拒绝**（卷换版并清理旧目录的瞬间不会
   读到跨版本组合）。
2. **两份文件都通过才生效**：JSON 解析、结构校验、超时取值、引用完整性
   （每个 `target_id` 都能在同一份投射的 targets 中找到）全部通过后，才在
   锁内一次性替换内存快照；任一步失败返回错误原因，**继续使用原配置**。
3. **不按目录名排新旧，时钟回拨也能生效**：版本目录名虽然是 kubelet
   时间戳，但节点时钟可能回拨，新投射的目录名会按字典序排在旧版本之前。
   因此服务**不做目录名字典序新旧比较**：只要 `..data` 当前指向的投射
   两份 JSON 校验通过，就整版生效。
4. **并发重读用“提交前复核投射身份”兜底**：读取时记录 `..data` 解析出的
   真实版本目录，校验完成、锁内提交前再解析一次 `..data`。若期间卷已经
   换到另一份投射，本次读到的就是过期候选：**现行配置原样保留**，手动
   `POST /admin/reload` 返回 **409**（响应说明切换方向，重试即可读到
   最新投射），后台轮询只在日志/`app.state.poll_status` 记下原因并继续
   运行。收尾较慢的旧请求不可能把已经生效的新目标换回去。
5. **查询不等重读**：现行快照是不可变对象，`GET` 接口只做原子引用读取，
   不与重读争锁；即使重读锁被长时间占用，查询也立即返回现行版本号、地址
   与超时。两次重读交错时，新投射可以先于旧请求结束生效。

## API

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 进程存活检查，始终 200，附带 `status` 与当前版本（无配置时 `not_loaded`） |
| `GET /version` | 现行版本号、组/目标数量、内容哈希；无生效配置时 503 |
| `GET /groups` | 全部巡检组名称 |
| `GET /groups/{name}` | 按组返回**现行版本号**及各目标的地址、超时 |
| `POST /admin/reload` | 重读投射卷并原子生效；校验/挂载问题返回 422，读取后卷又切换到另一份投射的并发冲突返回 409，响应体含中文原因 |

交互式文档：服务启动后访问 `/docs`。

## 用 Docker Compose 启动（单实例）

```bash
# 1) 先生成一份模拟投射卷
python3 scripts/kube_sim.py --root ./volumes/config init

# 2) 构建并启动
docker compose up -d --build
docker compose logs -f inspection-config

# 3) 调用示例
./scripts/api-examples.sh
```

## 模拟 `..data` 链接切换（无需 Docker，可直接对本机进程演练）

`scripts/kube_sim.py` 按 kubelet 的真实方式管理投射卷：时间戳版本目录 +
相对符号链接 + 原子 rename 替换 `..data`：

```bash
# 一键端到端演练（自动起 uvicorn，跑完自动收尾）
PYTHON=$(which python3) ./scripts/e2e-demo.sh

# 时钟回拨 + 并发重读专项演示（真实 uvicorn/curl + 确定性 TestClient 交错）
PYTHON=$(which python3) ./scripts/rollback-demo.sh

# 只看确定性的“慢旧请求 vs 新投射”交错（走现有 HTTP API，无需起端口）
python3 scripts/race_demo.py

# 或手动分步：
python3 scripts/kube_sim.py --root ./volumes/config init        # 发布 v1
curl -s -X POST localhost:8000/admin/reload                     # 首次加载
python3 scripts/kube_sim.py --root ./volumes/config publish v2  # 原子换版
curl -s -X POST localhost:8000/admin/reload                     # 免重启生效
# 时钟回拨：用字典序更早的目录名发布新内容，reload 仍应返回 200 并整版生效
python3 scripts/kube_sim.py --root ./volumes/config \
  publish-named '..2024-01-01T00_00_00.000000000-Z' v2
curl -s -X POST localhost:8000/admin/reload
python3 scripts/kube_sim.py --root ./volumes/config bad-swap    # 换入引用缺失版本
curl -s -X POST localhost:8000/admin/reload                     # 422 + 原因，旧配置保留
python3 scripts/kube_sim.py --root ./volumes/config prune       # 清理旧版本目录
python3 scripts/kube_sim.py --root ./volumes/config mixed-swap  # 制造两版文件混用
curl -s -X POST localhost:8000/admin/reload                     # 拒绝混用
python3 scripts/kube_sim.py --root ./volumes/config status
```

### 并发重读的 409 是怎么回事

`409` 只表示一种乐观并发冲突：某个手动重读线程已经读完某份投射并在校验，
这期间 `..data` 被 kubelet 换到了另一份投射、且新投射可能已经由另一次重读
生效。旧线程在锁内提交前复核失败，于是返回 409 且**不覆盖现行配置**；调用
方（或下一轮后台轮询）重新 reload 即会拿到最新投射。它与“目录名新旧”无关：
静态地把 `..data` 指回任意一份有效投射后调用 reload，都会正常 200 生效。

### API 调用示例

```bash
curl -s localhost:8000/health
curl -s localhost:8000/version
curl -s localhost:8000/groups/db-inspection | python3 -m json.tool
curl -s -X POST localhost:8000/admin/reload | python3 -m json.tool
```

按组查询响应示例：

```json
{
  "group": "db-inspection",
  "version": "..2026-11-27T00_00_14.300219000-Z",
  "targets": [
    {"id": "mysql-primary", "address": "10.0.1.10:3306", "timeout_seconds": 2.5},
    {"id": "mysql-replica",  "address": "10.0.1.11:3306", "timeout_seconds": 2.5}
  ]
}
```

## 部署到 Kubernetes

参考 `manifests/`：ConfigMap 以只读卷挂载到 `CONFIG_VOLUME_ROOT`。
更新 ConfigMap 后 kubelet 原子切换 `..data`（默认约 1 分钟内同步），
调用 `POST /admin/reload` 即可；也可设置 `CONFIG_WATCH_INTERVAL=2`
开启后台轮询自动重读。轮询遇到校验失败或投射切换冲突时不会退出，会以
`inspection-config` logger 的 WARNING 记下中文原因（冲突另记
“后台轮询遇到投射切换冲突”），并把最近一次结果写入
`app.state.poll_status`，期间始终继续提供现行配置。`livenessProbe` 用
`/health`，`readinessProbe` 用 `/version`（无生效配置时摘流但不重启）。

## 测试

```bash
pip install -r requirements.txt httpx pytest
python -m pytest          # 24 个用例：原子性、引用完整性、混用拒绝、时钟回拨、
                          # 提交前投射复核(409)、确定性并发交错、后台轮询记录、API 状态码
```

## 设计边界

- **单实例**：现行版本保存在进程内存。多副本部署时每个 Pod 各自持有
  快照，需要各自触发重读（或开启各自的轮询），本项目不做跨副本协调。
- **不依赖目录名判断新旧**：以 `..data` 提交前复核到的真实版本目录身份
  为准，因此节点时钟回拨、目录名字典序倒挂都不影响生效；唯一的并发拒绝
  （409）来自“读取后、提交前卷已换到另一份投射”。
- 未被任何巡检组引用的目标只产生 `warnings`，不阻止生效。
