import json
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import jwt
from aiohttp import web
from passlib.context import CryptContext

from db import create_pool, ensure_schema_async, seed_if_empty
from rules import judge_temp

SECRET = os.environ.get("JWT_SECRET", "coldchain-probe-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = {
    "logger": {"role": "writer", "password_hash": pwd.hash("log123456")},
    "logger2": {"role": "writer", "password_hash": pwd.hash("log2234567")},
    "watcher": {"role": "reader", "password_hash": pwd.hash("watch123456")},
}


def _auth_header(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _decode_user(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    sub = payload.get("sub")
    if sub not in USERS:
        return None
    return {"username": sub, "role": payload.get("role")}


def require_user(request: web.Request) -> dict:
    user = _decode_user(_auth_header(request))
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"detail": "未登录"}, ensure_ascii=False), content_type="application/json")
    return user


def require_writer(request: web.Request) -> dict:
    user = require_user(request)
    if user["role"] != "writer":
        raise web.HTTPForbidden(
            text=json.dumps({"detail": "仅记录员可操作"}, ensure_ascii=False),
            content_type="application/json",
        )
    return user


def _json_error(status: int, detail: str) -> web.HTTPException:
    cls = {400: web.HTTPBadRequest, 403: web.HTTPForbidden, 409: web.HTTPConflict}[status]
    return cls(
        text=json.dumps({"detail": detail}, ensure_ascii=False),
        content_type="application/json",
    )


def _parse_float(body, field: str, label: str) -> float:
    try:
        return float(body.get(field))
    except (TypeError, ValueError) as exc:
        raise _json_error(400, f"{label}必须是数字") from exc


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "coldchain-probe-desk"})


async def login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    user = USERS.get(username)
    if not user or not pwd.verify(password, user["password_hash"]):
        raise web.HTTPUnauthorized(
            text=json.dumps({"detail": "用户名或密码错误"}, ensure_ascii=False),
            content_type="application/json",
        )
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": username, "role": user["role"], "exp": exp},
        SECRET,
        algorithm="HS256",
    )
    return web.json_response(
        {"access_token": token, "username": username, "role": user["role"]}
    )


def _reading_out(r) -> dict:
    return {
        "id": r["id"],
        "probe_id": r["probe_id"],
        "temp_c": r["temp_c"],
        "verdict": r["verdict"],
        "reason": r["reason"],
        "status": r["status"],
        "created_by": r["created_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
    }


def _event_out(r) -> dict:
    return {
        "id": r["id"],
        "event_type": r["event_type"],
        "probe_id": r["probe_id"],
        "voltage": r["voltage"],
        "min_voltage": r["min_voltage"],
        "reading_id": r["reading_id"],
        "detail": r["detail"],
        "created_by": r["created_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


READING_COLS = "id, probe_id, temp_c, verdict, reason, status, created_by, created_at, processed_at"


async def list_readings(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(f"SELECT {READING_COLS} FROM probe_readings ORDER BY id DESC")
    return web.json_response([_reading_out(r) for r in rows])


async def create_reading(request: web.Request) -> web.Response:
    user = require_writer(request)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    probe_id = str(body.get("probe_id", "")).strip()
    if not probe_id:
        raise _json_error(400, "探头编号不能为空")
    temp_c = _parse_float(body, "temp_c", "温度")

    pool: asyncpg.Pool = request.app["pool"]
    # rejection 在事务内落库、事务提交后再抛出，确保拒收入账与流水不会随响应回滚。
    rejection: dict | None = None
    accepted_row = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 门槛行加共享锁：并发提交彼此不阻塞，但与门槛调整(FOR UPDATE)互斥，
            # 保证下面核对所用门槛值在本事务内稳定。
            await conn.fetchrow("SELECT min_voltage FROM voltage_threshold WHERE id = 1 FOR SHARE")
            # 锁该探头最近电压行：并发抢交在这把行锁上串行，电压维护也在此互斥。
            await conn.fetchrow(
                "SELECT voltage FROM probe_voltages WHERE probe_id = $1 FOR UPDATE",
                probe_id,
            )
            # 共用核对口径：只读 probe_voltage_check 这一处逻辑，拦截与监视簿同源。
            check = await conn.fetchrow(
                "SELECT has_record, voltage, min_voltage, blocked, reason "
                "FROM probe_voltage_check($1)",
                probe_id,
            )

            async def reject(verdict: str, event_type: str, detail: str, status: int) -> None:
                # 拒收入账 + 监视流水在同一事务内原子落库，要么都成要么都不成。
                rj = await conn.fetchrow(
                    f"""
                    INSERT INTO probe_readings
                        (probe_id, temp_c, verdict, reason, status, created_by, processed_at)
                    VALUES ($1, $2, $3, $4, 'rejected', $5, now())
                    RETURNING {READING_COLS}
                    """,
                    probe_id,
                    temp_c,
                    verdict,
                    detail,
                    user["username"],
                )
                await conn.execute(
                    """
                    INSERT INTO voltage_events
                        (event_type, probe_id, voltage, min_voltage, reading_id, detail, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    event_type,
                    probe_id,
                    check["voltage"],
                    check["min_voltage"],
                    rj["id"],
                    detail,
                    user["username"],
                )
                nonlocal rejection
                rejection = {"detail": detail, "status": status}

            if check["blocked"]:
                await reject("欠压拒收", "voltage_rejected", check["reason"], 400)
            else:
                # 电压核对通过：同探头至多一笔在途，唯一索引给并发抢交兜底。
                try:
                    async with conn.transaction():  # savepoint：撞车索引冲突后仍能登记拒收
                        accepted_row = await conn.fetchrow(
                            f"""
                            INSERT INTO probe_readings (probe_id, temp_c, status, created_by, created_at)
                            VALUES ($1, $2, 'pending', $3, now())
                            RETURNING {READING_COLS}
                            """,
                            probe_id,
                            temp_c,
                            user["username"],
                        )
                except asyncpg.UniqueViolationError:
                    accepted_row = None
                    collision = (
                        f"探头 {probe_id} 已有一笔在途读数（待处理/处理中），"
                        "同探至多一笔成，本笔当场拒收"
                    )
                    await reject("撞车拒收", "submit_rejected", collision, 409)

    # 事务已提交：拒收读数与监视流水此时已原子落库，再向客户端返回拒收响应。
    if rejection is not None:
        raise _json_error(rejection["status"], rejection["detail"])

    return web.json_response(
        {
            **_reading_out(accepted_row),
            "processed_at": None,
            "message": "已入队，后台工人将认领并判定",
        },
        status=201,
    )


async def voltage_monitor(request: web.Request) -> web.Response:
    """电压监视落地页三块数据：门槛电压表、各探头最近电压（含共用口径欠压标记）、监视流水。"""
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        t = await conn.fetchrow(
            "SELECT id, min_voltage, updated_by, updated_at "
            "FROM voltage_threshold WHERE id = 1"
        )
        threshold = None
        if t:
            threshold = {
                "min_voltage": t["min_voltage"],
                "updated_by": t["updated_by"],
                "updated_at": t["updated_at"].isoformat() if t["updated_at"] else None,
            }
        probes = await conn.fetch(
            """
            SELECT pv.probe_id, pv.voltage, pv.updated_by, pv.updated_at,
                   c.min_voltage AS threshold_voltage, c.blocked, c.reason
            FROM probe_voltages pv
            CROSS JOIN LATERAL probe_voltage_check(pv.probe_id) c
            ORDER BY pv.probe_id
            """
        )
        probe_out = [
            {
                "probe_id": r["probe_id"],
                "voltage": r["voltage"],
                "min_voltage": r["threshold_voltage"],
                "blocked": r["blocked"],
                "reason": r["reason"],
                "updated_by": r["updated_by"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in probes
        ]
        events = await conn.fetch(
            """
            SELECT id, event_type, probe_id, voltage, min_voltage, reading_id,
                   detail, created_by, created_at
            FROM voltage_events
            ORDER BY id DESC
            LIMIT 200
            """
        )
    return web.json_response(
        {"threshold": threshold, "probes": probe_out, "events": [_event_out(r) for r in events]}
    )


async def set_threshold(request: web.Request) -> web.Response:
    user = require_writer(request)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    min_voltage = _parse_float(body, "min_voltage", "最低电压门槛")
    if not 0 < min_voltage <= 12:
        raise _json_error(400, "最低电压门槛须在 0~12V 之间")

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                "SELECT min_voltage FROM voltage_threshold WHERE id = 1 FOR UPDATE"
            )
            await conn.execute(
                """
                INSERT INTO voltage_threshold (id, min_voltage, updated_by, updated_at)
                VALUES (1, $1, $2, now())
                ON CONFLICT (id) DO UPDATE
                    SET min_voltage = EXCLUDED.min_voltage,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = now()
                """,
                min_voltage,
                user["username"],
            )
            if old is None:
                detail = f"最低电压门槛设定为 {min_voltage:.2f}V"
            else:
                detail = (
                    f"最低电压门槛由 {old['min_voltage']:.2f}V 调整为 {min_voltage:.2f}V"
                )
            await conn.execute(
                """
                INSERT INTO voltage_events
                    (event_type, probe_id, voltage, min_voltage, detail, created_by)
                VALUES ('threshold_set', NULL, NULL, $1, $2, $3)
                """,
                min_voltage,
                detail,
                user["username"],
            )
    return web.json_response({"message": detail, "min_voltage": min_voltage})


async def set_probe_voltage(request: web.Request) -> web.Response:
    user = require_writer(request)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    probe_id = str(body.get("probe_id", "")).strip()
    if not probe_id:
        raise _json_error(400, "探头编号不能为空")
    voltage = _parse_float(body, "voltage", "电池电压")
    if not 0 < voltage <= 12:
        raise _json_error(400, "电池电压须在 0~12V 之间")

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                "SELECT voltage FROM probe_voltages WHERE probe_id = $1 FOR UPDATE",
                probe_id,
            )
            await conn.execute(
                """
                INSERT INTO probe_voltages (probe_id, voltage, updated_by, updated_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (probe_id) DO UPDATE
                    SET voltage = EXCLUDED.voltage,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = now()
                """,
                probe_id,
                voltage,
                user["username"],
            )
            check = await conn.fetchrow(
                "SELECT min_voltage, blocked, reason FROM probe_voltage_check($1)",
                probe_id,
            )
            if old is None:
                prefix = f"登记最近电池电压 {voltage:.2f}V"
            else:
                prefix = f"最近电池电压由 {old['voltage']:.2f}V 更新为 {voltage:.2f}V"
            if check and check["blocked"]:
                detail = f"{prefix}；{check['reason']}"
            else:
                detail = f"{prefix}，不低于门槛 {check['min_voltage']:.2f}V"
            await conn.execute(
                """
                INSERT INTO voltage_events
                    (event_type, probe_id, voltage, min_voltage, detail, created_by)
                VALUES ('voltage_update', $1, $2, $3, $4, $5)
                """,
                probe_id,
                voltage,
                check["min_voltage"] if check else None,
                detail,
                user["username"],
            )
    return web.json_response({"message": detail, "probe_id": probe_id, "voltage": voltage})


async def on_startup(app: web.Application) -> None:
    pool = await create_pool()
    app["pool"] = pool
    await ensure_schema_async(pool)
    await seed_if_empty(pool)


async def on_cleanup(app: web.Application) -> None:
    pool: asyncpg.Pool = app.get("pool")
    if pool:
        await pool.close()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/api/readings", list_readings)
    app.router.add_post("/api/readings", create_reading)
    app.router.add_get("/api/voltage/monitor", voltage_monitor)
    app.router.add_put("/api/voltage/threshold", set_threshold)
    app.router.add_put("/api/voltage/probe", set_probe_voltage)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8000)
