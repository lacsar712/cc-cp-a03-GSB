"""冷链探头读数判定：摄氏温度不超过 8 为合格，否则超温。

电压门槛核对口径：`judge_voltage` 是全系统唯一判定入口，
门槛判定（GET /api/voltage/probes）、提交拦截（POST /api/readings）、
电压监视簿（GET /api/voltage/events 及事件落库）三处共用同一函数，
任何一处另写口径即视为核对失败。
"""


def judge_temp(temp_c: float) -> tuple[str, str]:
    if temp_c <= 8:
        return "合格", "探头温度未超过 8℃ 上限"
    return "超温", "探头温度超过 8℃ 冷链上限"


def judge_voltage(voltage: float | None, min_voltage: float) -> tuple[bool, str]:
    """电压门槛唯一核对口径。

    返回 (是否放行, 核对说明)。电压低于门槛（含无电压记录）一律不放行；
    电压回升到门槛及以上才放行。门槛判定、提交拦截、电压监视簿三处
    必须共用本函数，保证同一电压同一门槛结论一致。
    """
    if voltage is None:
        return False, "该探头暂无电压记录，无法核对电压门槛"
    if voltage < min_voltage:
        return False, f"探头电压 {voltage:.2f}V 低于最低门槛 {min_voltage:.2f}V，禁止再交新温"
    return True, f"探头电压 {voltage:.2f}V 达到最低门槛 {min_voltage:.2f}V"


def verdict_for_display(verdict: str | None, status: str) -> str:
    if verdict:
        return verdict
    if status == "pending":
        return "待处理"
    if status == "processing":
        return "处理中"
    if status == "rejected":
        return "拒收"
    return "—"
