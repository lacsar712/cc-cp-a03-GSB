import json
import math
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import jwt
from aiohttp import web
from passlib.context import CryptContext

from db import create_pool, ensure_schema_async, seed_if_empty
from rules import judge_voltage

SECRET = os.environ.get("JWT_SECRET", "coldchain-probe-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = {
    "logger": {"role": "writer", "password_hash": pwd.hash("log123456")},
    "watcher": {"role": "reader", "password_hash": pwd.hash("watch123456")},
}

MAX_VOLTAGE = 100.0


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


_STATUS_EXC = {
    400: web.HTTPBadRequest,
    401: web.HTTPUnauthorized,
    403: web.HTTPForbidden,
    404: web.HTTPNotFound,
    409: web.HTTPConflict,
    422: web.HTTPUnprocessableEntity,
    500: web.HTTPInternalServerError,
}


def _json_error(status: int, detail: str) -> web.HTTPException:
    exc_cls = _STATUS_EXC.get(status, web.HTTPInternalServerError)
    return exc_cls(
        text=json.dumps({"detail": detail}, ensure_ascii=False),
        content_type="application/json",
    )


def require_user(request: web.Request) -> dict:
    user = _decode_user(_auth_header(request))
    if not user:
        raise _json_error(401, "未登录")
    return user


def require_writer(request: web.Request) -> dict:
    user = require_user(request)
    if user["role"] != "writer":
        raise _json_error(403, "仅记录员可执行写操作，观察账号只读")
    return user


async def _parse_json(request: web.Request) -> dict:
    try:
        return await request.json()
    except json.JSONDecodeError as exc:
        raise _json_error(400, "invalid json") from exc


def _parse_voltage(value, field: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise _json_error(400, f"{field}必须是数字") from exc
    if not math.isfinite(v) or v < 0 or v > MAX_VOLTAGE:
        raise _json_error(400, f"{field}必须在 0~{MAX_VOLTAGE:.0f}V 之间")
    return v


def _reading_out(row) -> dict:
    return {
        "id": row["id"],
        "probe_id": row["probe_id"],
        "temp_c": row["temp_c"],
        "verdict": row["verdict"],
        "reason": row["reason"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "processed_at": row["processed_at"].isoformat() if row["processed_at"] else None,
    }


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "coldchain-probe-desk"})


async def login(request: web.Request) -> web.Response:
    body = await _parse_json(request)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    user = USERS.get(username)
    if not user or not pwd.verify(password, user["password_hash"]):
        raise _json_error(401, "用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": username, "role": user["role"], "exp": exp},
        SECRET,
        algorithm="HS256",
    )
    return web.json_response(
        {"access_token": token, "username": username, "role": user["role"]}
    )


async def list_readings(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        """
        SELECT id, probe_id, temp_c, verdict, reason, status, created_by, created_at, processed_at
        FROM probe_readings
        ORDER BY id DESC
        """
    )
    return web.json_response([_reading_out(r) for r in rows])


async def create_reading(request: web.Request) -> web.Response:
    """提交读数：服务端在单事务内完成电压门槛拦截。

    同一探头按 advisory 锁串行，撞车抢交至多一笔成；电压低于门槛
    （共用 rules.judge_voltage 口径）或已有在途读数即整笔拒收。
    拒收读数入账与监视流水在同一事务落库，失败整体回滚不留半截。
    """
    user = require_writer(request)
    body = await _parse_json(request)
    probe_id = str(body.get("probe_id", "")).strip()
    if not probe_id:
        raise _json_error(400, "探头编号不能为空")
    try:
        temp_c = float(body.get("temp_c"))
    except (TypeError, ValueError) as exc:
        raise _json_error(400, "温度必须是数字") from exc
    if not math.isfinite(temp_c):
        raise _json_error(400, "温度必须是数字")

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 同一探头串行化：两名记录员撞车抢交时，后到者等锁后再核对，
            # 至多一笔放行，另一笔当场拒收。
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", probe_id)
            threshold = await conn.fetchval(
                "SELECT min_voltage FROM voltage_settings WHERE id = 1"
            )
            voltage = await conn.fetchval(
                "SELECT voltage FROM probe_voltages WHERE probe_id = $1", probe_id
            )
            ok, check_text = judge_voltage(voltage, threshold)
            if ok:
                inflight_id = await conn.fetchval(
                    """
                    SELECT id FROM probe_readings
                    WHERE probe_id = $1 AND status IN ('pending', 'processing')
                    ORDER BY id LIMIT 1
                    """,
                    probe_id,
                )
                if inflight_id is not None:
                    ok = False
                    check_text = (
                        f"该探头已有在途读数（编号 {inflight_id}）待判定，"
                        "本次撞车抢交当场拒收"
                    )
            status = "pending" if ok else "rejected"
            row = await conn.fetchrow(
                """
                INSERT INTO probe_readings
                    (probe_id, temp_c, status, reason, created_by, created_at, processed_at)
                VALUES ($1, $2, $3, $4, $5, now(), CASE WHEN $3 = 'rejected' THEN now() ELSE NULL END)
                RETURNING id, probe_id, temp_c, verdict, reason, status, created_by, created_at, processed_at
                """,
                probe_id,
                temp_c,
                status,
                None if ok else check_text,
                user["username"],
            )
            await conn.execute(
                """
                INSERT INTO voltage_monitor_events
                    (probe_id, event_type, voltage, threshold, detail, actor)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                probe_id,
                "accept" if ok else "reject",
                voltage,
                threshold,
                f"收下读数（编号 {row['id']}）：{check_text}" if ok else check_text,
                user["username"],
            )

    out = _reading_out(row)
    if not ok:
        out["detail"] = check_text
        return web.json_response(out, status=422)
    out["message"] = "已入队，后台工人将认领并判定"
    return web.json_response(out, status=201)


async def get_voltage_config(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    row = await pool.fetchrow(
        "SELECT min_voltage, updated_by, updated_at FROM voltage_settings WHERE id = 1"
    )
    if not row:
        raise _json_error(500, "电压门槛未初始化")
    return web.json_response(
        {
            "min_voltage": row["min_voltage"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    )


async def put_voltage_config(request: web.Request) -> web.Response:
    """记录员调整最低电压门槛；门槛落库与监视流水同事务。"""
    user = require_writer(request)
    body = await _parse_json(request)
    min_voltage = _parse_voltage(body.get("min_voltage"), "最低电压门槛")
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE voltage_settings
                SET min_voltage = $1, updated_by = $2, updated_at = now()
                WHERE id = 1
                RETURNING min_voltage, updated_by, updated_at
                """,
                min_voltage,
                user["username"],
            )
            if not row:
                raise _json_error(500, "电压门槛未初始化")
            await conn.execute(
                """
                INSERT INTO voltage_monitor_events
                    (probe_id, event_type, threshold, detail, actor)
                VALUES (NULL, 'threshold_change', $1, $2, $3)
                """,
                min_voltage,
                f"最低电压门槛调整为 {min_voltage:.2f}V",
                user["username"],
            )
    return web.json_response(
        {
            "min_voltage": row["min_voltage"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    )


async def get_voltage_probes(request: web.Request) -> web.Response:
    """探头电压表：每行核对结论由共用的 judge_voltage 口径算出。"""
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    threshold = await pool.fetchval(
        "SELECT min_voltage FROM voltage_settings WHERE id = 1"
    )
    rows = await pool.fetch(
        """
        WITH probes AS (
            SELECT probe_id FROM probe_voltages
            UNION
            SELECT probe_id FROM probe_readings
        )
        SELECT p.probe_id, v.voltage, v.updated_by, v.updated_at
        FROM probes p
        LEFT JOIN probe_voltages v ON v.probe_id = p.probe_id
        ORDER BY p.probe_id
        """
    )
    out = []
    for r in rows:
        ok, check_text = judge_voltage(r["voltage"], threshold)
        out.append(
            {
                "probe_id": r["probe_id"],
                "voltage": r["voltage"],
                "min_voltage": threshold,
                "ok": ok,
                "check": check_text,
                "updated_by": r["updated_by"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
        )
    return web.json_response(out)


async def put_probe_voltage(request: web.Request) -> web.Response:
    """记录员登记/维护某探头最近电压；落库与监视流水同事务。"""
    user = require_writer(request)
    probe_id = request.match_info["probe_id"].strip()
    if not probe_id:
        raise _json_error(400, "探头编号不能为空")
    body = await _parse_json(request)
    voltage = _parse_voltage(body.get("voltage"), "电压")
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            threshold = await conn.fetchval(
                "SELECT min_voltage FROM voltage_settings WHERE id = 1"
            )
            row = await conn.fetchrow(
                """
                INSERT INTO probe_voltages (probe_id, voltage, updated_by, updated_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (probe_id) DO UPDATE
                SET voltage = EXCLUDED.voltage,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                RETURNING probe_id, voltage, updated_by, updated_at
                """,
                probe_id,
                voltage,
                user["username"],
            )
            ok, check_text = judge_voltage(voltage, threshold)
            await conn.execute(
                """
                INSERT INTO voltage_monitor_events
                    (probe_id, event_type, voltage, threshold, detail, actor)
                VALUES ($1, 'voltage_change', $2, $3, $4, $5)
                """,
                probe_id,
                voltage,
                threshold,
                f"登记最近电压：{check_text}",
                user["username"],
            )
    return web.json_response(
        {
            "probe_id": row["probe_id"],
            "voltage": row["voltage"],
            "min_voltage": threshold,
            "ok": ok,
            "check": check_text,
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    )


async def get_voltage_events(request: web.Request) -> web.Response:
    """电压监视簿：流水逐笔留痕，核对结论同样走 judge_voltage 口径。"""
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        """
        SELECT id, probe_id, event_type, voltage, threshold, detail, actor, created_at
        FROM voltage_monitor_events
        ORDER BY id DESC
        LIMIT 200
        """
    )
    out = []
    for r in rows:
        ok = None
        check = None
        if r["voltage"] is not None and r["threshold"] is not None:
            ok, check = judge_voltage(r["voltage"], r["threshold"])
        out.append(
            {
                "id": r["id"],
                "probe_id": r["probe_id"],
                "event_type": r["event_type"],
                "voltage": r["voltage"],
                "threshold": r["threshold"],
                "ok": ok,
                "check": check,
                "detail": r["detail"],
                "actor": r["actor"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return web.json_response(out)


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
    app.router.add_get("/api/voltage/config", get_voltage_config)
    app.router.add_put("/api/voltage/config", put_voltage_config)
    app.router.add_get("/api/voltage/probes", get_voltage_probes)
    app.router.add_put("/api/voltage/probes/{probe_id}", put_probe_voltage)
    app.router.add_get("/api/voltage/events", get_voltage_events)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8000)
