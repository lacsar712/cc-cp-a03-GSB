import os

import asyncpg
import psycopg
from psycopg.rows import dict_row

from rules import judge_temp

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:54397/coldchain"
)

# 出厂默认最低电压门槛（伏），记录员可在电压监视页调整
DEFAULT_MIN_VOLTAGE = 3.3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS probe_readings (
    id serial PRIMARY KEY,
    probe_id text NOT NULL,
    temp_c double precision NOT NULL,
    verdict text,
    reason text,
    status text NOT NULL DEFAULT 'pending',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_probe_readings_status ON probe_readings (status, id);
CREATE INDEX IF NOT EXISTS idx_probe_readings_probe ON probe_readings (probe_id, status);

-- 电压门槛：单行单例（id 恒为 1）
CREATE TABLE IF NOT EXISTS voltage_settings (
    id smallint PRIMARY KEY CHECK (id = 1),
    min_voltage double precision NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 各探头最近电压，一探一行
CREATE TABLE IF NOT EXISTS probe_voltages (
    probe_id text PRIMARY KEY,
    voltage double precision NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 电压监视流水：收下 / 拒收 / 调门槛 / 登记电压，逐笔留痕
CREATE TABLE IF NOT EXISTS voltage_monitor_events (
    id serial PRIMARY KEY,
    probe_id text,
    event_type text NOT NULL,
    voltage double precision,
    threshold double precision,
    detail text NOT NULL,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_voltage_monitor_events_id ON voltage_monitor_events (id DESC);
"""

SEED_VOLTAGES = [
    ("探头A01", 3.72),
    ("探头B02", 3.85),
]

SEED_READINGS = [
    ("探头A01", 4.2),
    ("探头B02", 12.5),
]


def connect_sync():
    return psycopg.connect(DSN, row_factory=dict_row)


def ensure_schema_sync(conn) -> None:
    conn.execute(SCHEMA_SQL)


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DSN, min_size=1, max_size=5)


async def ensure_schema_async(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def seed_if_empty(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        # 门槛单例缺则补（不覆盖记录员已调的值）
        await conn.execute(
            """
            INSERT INTO voltage_settings (id, min_voltage, updated_by)
            VALUES (1, $1, 'system')
            ON CONFLICT (id) DO NOTHING
            """,
            DEFAULT_MIN_VOLTAGE,
        )
        # 探头电压空表才补种子
        nv = await conn.fetchval("SELECT COUNT(*) FROM probe_voltages")
        if not nv:
            for probe_id, voltage in SEED_VOLTAGES:
                await conn.execute(
                    """
                    INSERT INTO probe_voltages (probe_id, voltage, updated_by)
                    VALUES ($1, $2, 'system')
                    ON CONFLICT (probe_id) DO NOTHING
                    """,
                    probe_id,
                    voltage,
                )
        n = await conn.fetchval("SELECT COUNT(*) FROM probe_readings")
        if n and n > 0:
            return
        for probe_id, temp_c in SEED_READINGS:
            verdict, reason = judge_temp(temp_c)
            await conn.execute(
                """
                INSERT INTO probe_readings
                    (probe_id, temp_c, verdict, reason, status, created_by, processed_at)
                VALUES ($1, $2, $3, $4, 'done', 'logger', now())
                """,
                probe_id,
                temp_c,
                verdict,
                reason,
            )


def seed_if_empty_sync(conn) -> None:
    conn.execute(
        """
        INSERT INTO voltage_settings (id, min_voltage, updated_by)
        VALUES (1, %s, 'system')
        ON CONFLICT (id) DO NOTHING
        """,
        (DEFAULT_MIN_VOLTAGE,),
    )
    row = conn.execute("SELECT COUNT(*) AS n FROM probe_voltages").fetchone()
    if row["n"] == 0:
        for probe_id, voltage in SEED_VOLTAGES:
            conn.execute(
                """
                INSERT INTO probe_voltages (probe_id, voltage, updated_by)
                VALUES (%s, %s, 'system')
                ON CONFLICT (probe_id) DO NOTHING
                """,
                (probe_id, voltage),
            )
    row = conn.execute("SELECT COUNT(*) AS n FROM probe_readings").fetchone()
    if row["n"] == 0:
        for probe_id, temp_c in SEED_READINGS:
            verdict, reason = judge_temp(temp_c)
            conn.execute(
                """
                INSERT INTO probe_readings
                    (probe_id, temp_c, verdict, reason, status, created_by, processed_at)
                VALUES (%s, %s, %s, %s, 'done', 'logger', now())
                """,
                (probe_id, temp_c, verdict, reason),
            )
    conn.commit()
