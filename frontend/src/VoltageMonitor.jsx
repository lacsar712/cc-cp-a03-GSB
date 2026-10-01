import { useCallback, useEffect, useState } from "preact/hooks";

const EVENT_TEXT = {
  threshold_set: "门槛设定",
  voltage_update: "电压登记",
  voltage_rejected: "欠压拒收",
  submit_rejected: "撞车拒收",
};

function eventClass(type) {
  if (type === "voltage_rejected" || type === "submit_rejected") return "tag fail";
  if (type === "threshold_set") return "tag threshold";
  return "tag wait";
}

function fmtTime(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 19);
}

export function VoltageMonitor({ token, user, authHeaders, onToast }) {
  const isWriter = user?.role === "writer";
  const [data, setData] = useState({ threshold: null, probes: [], events: [] });
  const [thresholdInput, setThresholdInput] = useState("");
  const [probeForm, setProbeForm] = useState({ probe_id: "", voltage: "" });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const res = await fetch("/api/voltage/monitor", { headers: authHeaders() });
    if (res.ok) setData(await res.json());
  }, [authHeaders]);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    if (data.threshold && thresholdInput === "") {
      setThresholdInput(String(data.threshold.min_voltage));
    }
  }, [data.threshold, thresholdInput]);

  async function saveThreshold(e) {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await fetch("/api/voltage/threshold", {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({ min_voltage: parseFloat(thresholdInput) }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) return onToast(body.detail || "门槛设定失败", true);
      onToast(body.message || "门槛已设定", false);
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function saveProbeVoltage(e) {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await fetch("/api/voltage/probe", {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({
          probe_id: probeForm.probe_id.trim(),
          voltage: parseFloat(probeForm.voltage),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) return onToast(body.detail || "电压维护失败", true);
      onToast(body.message || "电压已登记", false);
      setProbeForm({ probe_id: "", voltage: "" });
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div class="card">
        <h2 class="panel-title" style={{ marginTop: 0 }}>电压监视</h2>
        <p class="sub" style={{ marginBottom: 0 }}>
          提交拦截与本监视簿共用同一核对口径：探头有最近电压且不低于最低门槛方可报温，
          欠压读数整笔拒收并在流水留痕，电压回升后方可再交。
        </p>
      </div>

      <div class="panels">
        {/* 第一块：门槛电压表 */}
        <section class="card panel">
          <h3>门槛电压表</h3>
          {data.threshold ? (
            <div class="kv">
              <div>
                <span class="kv-label">最低电压</span>
                <strong class="big">{Number(data.threshold.min_voltage).toFixed(2)}V</strong>
              </div>
              <div class="kv-meta">设定人：{data.threshold.updated_by}</div>
              <div class="kv-meta">{fmtTime(data.threshold.updated_at)}</div>
            </div>
          ) : (
            <p class="err">尚未设定门槛</p>
          )}
          {isWriter ? (
            <form onSubmit={saveThreshold} class="mini-form">
              <label>
                调整最低电压（V）
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  max="12"
                  value={thresholdInput}
                  onInput={(e) => setThresholdInput(e.target.value)}
                />
              </label>
              <button type="submit" disabled={busy}>设定门槛</button>
            </form>
          ) : (
            <p class="readonly-hint">观察账号只读，不可设定门槛</p>
          )}
        </section>

        {/* 第二块：各探头最近电压 */}
        <section class="card panel">
          <h3>各探头最近电压</h3>
          <table class="compact">
            <thead>
              <tr>
                <th>探头</th>
                <th>电压</th>
                <th>核对</th>
              </tr>
            </thead>
            <tbody>
              {data.probes.map((p) => (
                <tr key={p.probe_id}>
                  <td>{p.probe_id}</td>
                  <td class={p.blocked ? "volt-low" : "volt-ok"}>
                    {Number(p.voltage).toFixed(2)}V
                  </td>
                  <td>
                    <span class={p.blocked ? "tag fail" : "tag pass"}>
                      {p.blocked ? "欠压" : "正常"}
                    </span>
                  </td>
                </tr>
              ))}
              {data.probes.length === 0 && (
                <tr><td colspan="3">暂无探头电压</td></tr>
              )}
            </tbody>
          </table>
          {isWriter ? (
            <form onSubmit={saveProbeVoltage} class="mini-form">
              <label>
                探头编号
                <input
                  value={probeForm.probe_id}
                  onInput={(e) => setProbeForm({ ...probeForm, probe_id: e.target.value })}
                  placeholder="例如 探头A01"
                />
              </label>
              <label>
                最近电压（V）
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  max="12"
                  value={probeForm.voltage}
                  onInput={(e) => setProbeForm({ ...probeForm, voltage: e.target.value })}
                />
              </label>
              <button type="submit" disabled={busy}>维护电压</button>
            </form>
          ) : (
            <p class="readonly-hint">观察账号只读，不可维护电压</p>
          )}
        </section>

        {/* 第三块：监视流水 */}
        <section class="card panel">
          <h3>监视流水</h3>
          <div class="event-list">
            {data.events.map((ev) => (
              <div key={ev.id} class="event-item">
                <div class="event-head">
                  <span class={eventClass(ev.event_type)}>
                    {EVENT_TEXT[ev.event_type] || ev.event_type}
                  </span>
                  {ev.probe_id && <span class="event-probe">{ev.probe_id}</span>}
                  <span class="event-time">{fmtTime(ev.created_at)}</span>
                </div>
                <div class="event-detail">{ev.detail}</div>
                <div class="event-meta">{ev.created_by}</div>
              </div>
            ))}
            {data.events.length === 0 && <p class="sub">暂无流水</p>}
          </div>
        </section>
      </div>
    </div>
  );
}
