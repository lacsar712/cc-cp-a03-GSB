# 冷链探头超温台

记录员上报探头编号与摄氏温度，后台工人用数据库行锁认领待处理队列，按 **8℃** 上限判定 **合格** 或 **超温**。

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
| logger | log123456 | 记录员，可提交读数、设门槛、维护电压 |
| logger2 | log2234567 | 第二名记录员，权限同 logger（用于并发抢交） |
| watcher | watch123456 | 观察账号，只读读数与电压监视三块 |

## 电池电压门槛

探头电池电压低于**最低电压门槛**时禁止再交新温：

- 记录员在「电压监视」落地页设定最低电压门槛，并维护各探头最近电压；
- 提交前服务端按统一核对口径（数据库函数 `probe_voltage_check`）比对：缺门槛、缺该探头最近电压记录、或最近电压低于门槛，任一成立即**整笔拒收**并列出原因，电压回升后方可再交；
- 门槛判定、监视簿欠压标记、拒收流水三处共用同一核对口径；
- 拒收入账与监视流水在**同一事务**原子落库，不会半截；
- 同一探头至多一笔在途读数（pending/processing），两名记录员临界抢交时由行锁 + 唯一索引保证**至多一笔成、另一笔当场拒收**；
- 观察账号可看门槛电压表、各探头最近电压、监视流水三块，但不能报温。

顶栏「电压监视」进入独立落地页，三块（门槛电压表 / 各探头最近电压 / 监视流水）并排展示，不是弹窗。

## 启动

```bash
cd projects/18-coldchain-probe-desk
docker compose up --build
```

健康检查：`GET http://localhost:8197/api/health` → `{"status":"ok","service":"coldchain-probe-desk"}`

## 种子数据

| 探头 | 温度 | 结论 |
|------|------|------|
| 探头A01 | 4.2℃ | 合格 |
| 探头B02 | 12.5℃ | 超温 |

## 本地开发（可选）

```bash
# 需本机 PostgreSQL 或仅起 db 容器
cd backend && pip install -r requirements.txt && python api.py
cd backend && python worker.py
cd frontend && npm install && npm run dev
```

接口进程默认监听容器内 **8000**，对外映射 **8197**。
