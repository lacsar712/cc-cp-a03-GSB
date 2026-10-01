import os

import asyncpg
import psycopg
from psycopg.rows import dict_row

from rules import judge_temp

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:54397/coldchain"
)

# 默认最低电池电压门槛（伏特）
DEFAULT_MIN_VOLTAGE = 3.3
# 种子探头的最近电池电压（伏特，均高于门槛）
SEED_PROBE_VOLTAGES = {
    "探头A01": 3.8,
    "探头B02": 3.6,
}

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

-- 同一探头至多一笔在途读数（pending/processing），并发抢交的硬约束
CREATE UNIQUE INDEX IF NOT EXISTS uq_probe_reading_active
    ON probe_readings (probe_id)
    WHERE status IN ('pending', 'processing');

-- 最低电池电压门槛（全局单行，id 恒为 1）
CREATE TABLE IF NOT EXISTS voltage_threshold (
    id smallint PRIMARY KEY DEFAULT 1,
    min_voltage double precision NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT voltage_threshold_singleton CHECK (id = 1)
);

-- 各探头最近一次电池电压（记录员维护）
CREATE TABLE IF NOT EXISTS probe_voltages (
    probe_id text PRIMARY KEY,
    voltage double precision NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 电压监视流水：门槛设定 / 电压登记 / 欠压拒收，只追加，不删改
CREATE TABLE IF NOT EXISTS voltage_events (
    id serial PRIMARY KEY,
    event_type text NOT NULL,
    probe_id text,
    voltage double precision,
    min_voltage double precision,
    reading_id integer REFERENCES probe_readings(id) ON DELETE SET NULL,
    detail text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_voltage_events_id ON voltage_events (id DESC);

-- 三处共用核对口径：提交拦截、监视簿欠压标记、拒收流水原因全部只认这个函数。
-- 规则：有最近电压记录且 voltage < min_voltage 即为欠压；等号不算欠压。
CREATE OR REPLACE FUNCTION probe_voltage_check(p_probe_id text)
RETURNS TABLE (
    has_record boolean,
    voltage double precision,
    min_voltage double precision,
    blocked boolean,
    reason text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_voltage double precision;
    v_min double precision;
    v_has boolean;
BEGIN
    SELECT vt.min_voltage INTO v_min
    FROM voltage_threshold vt
    WHERE vt.id = 1;

    SELECT pv.voltage INTO v_voltage
    FROM probe_voltages pv
    WHERE pv.probe_id = p_probe_id;

    v_has := v_voltage IS NOT NULL;

    RETURN QUERY
    SELECT
        v_has,
        v_voltage,
        v_min,
        -- 少一环即失败：缺门槛、缺最近电压记录、或电压低于门槛，任一成立即拦截
        (v_min IS NULL OR NOT v_has OR v_voltage < v_min),
        CASE
            WHEN v_min IS NULL
                THEN '尚未设定最低电压门槛，无法核对，禁止提交新温'
            WHEN NOT v_has
                THEN '缺少该探头最近电池电压记录，无法核对，禁止提交新温'
            WHEN v_voltage < v_min
                THEN '探头电池电压 ' || to_char(v_voltage, 'FM9990.00')
                     || 'V 低于最低电压门槛 ' || to_char(v_min, 'FM9990.00')
                     || 'V，整笔拒收，电压回升后方可再交'
            ELSE NULL
        END;
END;
$$;
"""


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
        await _seed_readings_async(conn)
        await _seed_voltage_async(conn)


def seed_if_empty_sync(conn) -> None:
    _seed_readings_sync(conn)
    _seed_voltage_sync(conn)
    conn.commit()


async def _seed_readings_async(conn) -> None:
    n = await conn.fetchval("SELECT COUNT(*) FROM probe_readings")
    if n and n > 0:
        return
    for probe_id, temp_c in [("探头A01", 4.2), ("探头B02", 12.5)]:
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


def _seed_readings_sync(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM probe_readings").fetchone()
    if row["n"] > 0:
        return
    for probe_id, temp_c in [("探头A01", 4.2), ("探头B02", 12.5)]:
        verdict, reason = judge_temp(temp_c)
        conn.execute(
            """
            INSERT INTO probe_readings
                (probe_id, temp_c, verdict, reason, status, created_by, processed_at)
            VALUES (%s, %s, %s, %s, 'done', 'logger', now())
            """,
            (probe_id, temp_c, verdict, reason),
        )


async def _seed_voltage_async(conn) -> None:
    seeded = await conn.fetchval(
        """
        INSERT INTO voltage_threshold (id, min_voltage, updated_by, updated_at)
        VALUES (1, $1, 'logger', now())
        ON CONFLICT (id) DO NOTHING
        RETURNING id
        """,
        DEFAULT_MIN_VOLTAGE,
    )
    if seeded is not None:
        await conn.execute(
            """
            INSERT INTO voltage_events
                (event_type, probe_id, voltage, min_voltage, detail, created_by)
            VALUES ('threshold_set', NULL, NULL, $1, $2, 'logger')
            """,
            DEFAULT_MIN_VOLTAGE,
            f"初始最低电压门槛设定为 {DEFAULT_MIN_VOLTAGE:.2f}V",
        )
    for probe_id, voltage in SEED_PROBE_VOLTAGES.items():
        inserted = await conn.fetchval(
            """
            INSERT INTO probe_voltages (probe_id, voltage, updated_by, updated_at)
            VALUES ($1, $2, 'logger', now())
            ON CONFLICT (probe_id) DO NOTHING
            RETURNING probe_id
            """,
            probe_id,
            voltage,
        )
        if inserted is not None:
            await conn.execute(
                """
                INSERT INTO voltage_events
                    (event_type, probe_id, voltage, min_voltage, detail, created_by)
                SELECT 'voltage_update', $1, $2, min_voltage, $3, 'logger'
                FROM voltage_threshold WHERE id = 1
                """,
                probe_id,
                voltage,
                f"初始登记最近电池电压 {voltage:.2f}V",
            )


def _seed_voltage_sync(conn) -> None:
    row = conn.execute(
        "INSERT INTO voltage_threshold (id, min_voltage, updated_by, updated_at) "
        "VALUES (1, %s, 'logger', now()) ON CONFLICT (id) DO NOTHING RETURNING id",
        (DEFAULT_MIN_VOLTAGE,),
    ).fetchone()
    if row is not None:
        conn.execute(
            """
            INSERT INTO voltage_events
                (event_type, probe_id, voltage, min_voltage, detail, created_by)
            VALUES ('threshold_set', NULL, NULL, %s, %s, 'logger')
            """,
            (DEFAULT_MIN_VOLTAGE, f"初始最低电压门槛设定为 {DEFAULT_MIN_VOLTAGE:.2f}V"),
        )
    for probe_id, voltage in SEED_PROBE_VOLTAGES.items():
        row = conn.execute(
            """
            INSERT INTO probe_voltages (probe_id, voltage, updated_by, updated_at)
            VALUES (%s, %s, 'logger', now())
            ON CONFLICT (probe_id) DO NOTHING
            RETURNING probe_id
            """,
            (probe_id, voltage),
        ).fetchone()
        if row is not None:
            conn.execute(
                """
                INSERT INTO voltage_events
                    (event_type, probe_id, voltage, min_voltage, detail, created_by)
                SELECT 'voltage_update', %s, %s, min_voltage, %s, 'logger'
                FROM voltage_threshold WHERE id = 1
                """,
                (probe_id, voltage, f"初始登记最近电池电压 {voltage:.2f}V"),
            )
