# 冷链探头超温台

记录员上报探头编号与摄氏温度，后台工人用数据库行锁认领待处理队列，按 **8℃** 上限判定 **合格** 或 **超温**。

**电压门槛**：探头电池电压低于最低门槛时禁止再交新温。记录员在「电压监视」专页设定最低电压门槛并维护各探头最近电压；提交读数前服务端按统一核对口径比对电压，低于门槛即列出原因并**整笔拒收**（拒收读数入账 + 监视流水，同事务原子落库），电压回升到门槛及以上才许再交，旧拒收记录在监视流水中留痕。

## 技术栈

| 层 | 选型 |
|----|------|
| 接口 | Python aiohttp + asyncpg |
| 工人 | `worker.py`（psycopg，`FOR UPDATE SKIP LOCKED`） |
| 页面 | Preact + Vite，nginx 反代 `/api` |
| 数据库 | PostgreSQL 16 |

## 端口

| 服务 | 地址 |
|------|------|
| 页面 | http://localhost:3197 |
| 接口 | http://localhost:8197 |
| PostgreSQL | localhost:54397（库名 `coldchain`） |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| logger | log123456 | 记录员，可提交读数、调门槛、登记探头电压 |
| watcher | watch123456 | 观察账号，只读（报温台与电压监视三块都能看，不能报温/改门槛/改电压） |

## 页面

顶栏导航两个专页（hash 路由，非弹窗）：

- **报温台**（`#/`）：提交读数（仅记录员）+ 读数列表（含被拒收记录，结论列显示「拒收」）。
- **电压监视**（`#/voltage`）：三块并排——电压门槛（记录员可调）、探头电压表（记录员可逐探登记/新增）、监视流水（收下/拒收/调门槛/登记电压逐笔留痕）。观察账号三块均只读。

## 电压核对口径（三处共用）

`backend/rules.py` 的 `judge_voltage(voltage, min_voltage)` 是唯一判定入口，三处共用同一口径，少一环即失败：

1. **门槛判定**：`GET /api/voltage/probes` 每行的 达标/低压 结论；
2. **提交拦截**：`POST /api/readings` 事务内比对，低于门槛整笔拒收；
3. **电压监视簿**：`GET /api/voltage/events` 流水逐笔的核对结论（含拒收快照）。

规则：无电压记录 → 拒收；电压 < 门槛 → 拒收；电压 ≥ 门槛 → 放行。

## 接口

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| POST | `/api/auth/login` | 公开 | 登录取 token |
| GET | `/api/readings` | 登录 | 读数列表（含 rejected） |
| POST | `/api/readings` | 记录员 | 提交读数；电压低于门槛/撞车在途 → 422 整笔拒收并留痕 |
| GET | `/api/voltage/config` | 登录 | 当前最低电压门槛 |
| PUT | `/api/voltage/config` | 记录员 | 调整门槛 `{min_voltage}` |
| GET | `/api/voltage/probes` | 登录 | 各探头最近电压 + 核对结论 |
| PUT | `/api/voltage/probes/{probe_id}` | 记录员 | 登记/更新探头电压 `{voltage}` |
| GET | `/api/voltage/events` | 登录 | 监视流水（最近 200 笔） |

## 并发与原子性

- **撞车抢交**：`POST /api/readings` 在事务内先取 `pg_advisory_xact_lock(hashtext(probe_id))`，同一探头串行核对；两名记录员同时抢交同一探头至多一笔放行，另一笔（发现已有 pending/processing 在途读数）当场拒收。
- **原子落库**：拒收读数入账（`probe_readings` status=rejected）与监视流水（`voltage_monitor_events`）在同一事务提交，任一失败整体回滚，不留半截。

## 启动

```bash
cd projects/18-coldchain-probe-desk
docker compose up --build
```

健康检查：`GET http://localhost:8197/api/health` → `{"status":"ok","service":"coldchain-probe-desk"}`

## 种子数据

| 探头 | 温度 | 结论 | 最近电压 | 门槛 |
|------|------|------|----------|------|
| 探头A01 | 4.2℃ | 合格 | 3.72V | 3.30V（默认） |
| 探头B02 | 12.5℃ | 超温 | 3.85V | 3.30V（默认） |

## 本地开发（可选）

```bash
# 需本机 PostgreSQL 或仅起 db 容器
cd backend && pip install -r requirements.txt && python api.py
cd backend && python worker.py
cd frontend && npm install && npm run dev
```

接口进程默认监听容器内 **8000**，对外映射 **8197**。
