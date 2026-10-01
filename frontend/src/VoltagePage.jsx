import { useCallback, useEffect, useState } from "preact/hooks";

const EVENT_LABELS = {
  accept: "收下",
  reject: "拒收",
  threshold_change: "调门槛",
  voltage_change: "登记电压",
};

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function fmtVolt(v) {
  return v === null || v === undefined ? "—" : `${Number(v).toFixed(2)}V`;
}

/** 电压监视专页：门槛设置、探头电压表、监视流水三块并排。 */
export function VoltagePage({ token, isWriter, authHeaders }) {
  const [config, setConfig] = useState(null);
  const [probes, setProbes] = useState([]);
  const [events, setEvents] = useState([]);
  const [thresholdInput, setThresholdInput] = useState("");
  const [voltInputs, setVoltInputs] = useState({});
  const [newProbe, setNewProbe] = useState({ probe_id: "", voltage: "" });
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    if (!token) return;
    const [c, p, e] = await Promise.all([
      fetch("/api/voltage/config", { headers: authHeaders() }),
      fetch("/api/voltage/probes", { headers: authHeaders() }),
      fetch("/api/voltage/events", { headers: authHeaders() }),
    ]);
    if (c.ok) setConfig(await c.json());
    if (p.ok) setProbes(await p.json());
    if (e.ok) setEvents(await e.json());
  }, [token, authHeaders]);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  async function putJson(url, body) {
    const res = await fetch(url, {
      method: "PUT",
      headers: authHeaders(),
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "保存失败");
    return data;
  }

  async function run(action) {
    setError("");
    setMsg("");
    try {
      await action();
      await load();
    } catch (err) {
      setError(err.message || "操作失败");
    }
  }

  function onSaveThreshold(e) {
    e.preventDefault();
    const v = parseFloat(thresholdInput);
    if (Number.isNaN(v)) {
      setError("最低电压门槛必须是数字");
      return;
    }
    run(async () => {
      const data = await putJson("/api/voltage/config", { min_voltage: v });
      setMsg(`最低电压门槛已调整为 ${Number(data.min_voltage).toFixed(2)}V`);
      setThresholdInput("");
    });
  }

  function onSaveVoltage(probeId) {
    const raw = voltInputs[probeId];
    const v = parseFloat(raw);
    if (raw === undefined || raw === "" || Number.isNaN(v)) {
      setError(`请先为 ${probeId} 填写数字电压`);
      return;
    }
    run(async () => {
      await putJson(`/api/voltage/probes/${encodeURIComponent(probeId)}`, { voltage: v });
      setMsg(`${probeId} 最近电压已登记为 ${v.toFixed(2)}V`);
      setVoltInputs({ ...voltInputs, [probeId]: "" });
    });
  }

  function onAddProbe(e) {
    e.preventDefault();
    const probeId = newProbe.probe_id.trim();
    const v = parseFloat(newProbe.voltage);
    if (!probeId) {
      setError("探头编号不能为空");
      return;
    }
    if (Number.isNaN(v)) {
      setError("电压必须是数字");
      return;
    }
    run(async () => {
      await putJson(`/api/voltage/probes/${encodeURIComponent(probeId)}`, { voltage: v });
      setMsg(`${probeId} 最近电压已登记为 ${v.toFixed(2)}V`);
      setNewProbe({ probe_id: "", voltage: "" });
    });
  }

  return (
    <div class="grid3">
      <div class="card">
        <h2 class="cardtitle">电压门槛</h2>
        <p class="big">{config ? fmtVolt(config.min_voltage) : "…"}</p>
        <p class="sub">
          探头最近电压低于该门槛即整笔拒收新读数，电压回升到门槛及以上才许再交。
        </p>
        <p class="meta">
          最近由 {config?.updated_by || "—"} 于 {fmtTime(config?.updated_at)} 调整
        </p>
        {isWriter ? (
          <form onSubmit={onSaveThreshold}>
            <div class="row">
              <label>
                最低电压（V）
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  max="100"
                  required
                  value={thresholdInput}
                  onInput={(e) => setThresholdInput(e.target.value)}
                  placeholder={config ? String(config.min_voltage) : "例如 3.30"}
                />
              </label>
              <button type="submit">保存门槛</button>
            </div>
          </form>
        ) : (
          <p class="meta">观察账号只读，门槛由记录员维护。</p>
        )}
        {error && <p class="err">{error}</p>}
        {msg && <p class="ok">{msg}</p>}
      </div>

      <div class="card">
        <h2 class="cardtitle">探头电压表</h2>
        <table>
          <thead>
            <tr>
              <th>探头</th>
              <th>最近电压</th>
              <th>核对</th>
              {isWriter && <th>登记</th>}
            </tr>
          </thead>
          <tbody>
            {probes.map((p) => (
              <tr key={p.probe_id}>
                <td>{p.probe_id}</td>
                <td>{fmtVolt(p.voltage)}</td>
                <td>
                  <span class={p.ok ? "tag pass" : "tag fail"} title={p.check}>
                    {p.ok ? "达标" : "低压"}
                  </span>
                </td>
                {isWriter && (
                  <td>
                    <span class="inline-edit">
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        max="100"
                        class="volt-input"
                        placeholder="电压V"
                        value={voltInputs[p.probe_id] || ""}
                        onInput={(e) =>
                          setVoltInputs({ ...voltInputs, [p.probe_id]: e.target.value })
                        }
                      />
                      <button type="button" onClick={() => onSaveVoltage(p.probe_id)}>
                        保存
                      </button>
                    </span>
                  </td>
                )}
              </tr>
            ))}
            {probes.length === 0 && (
              <tr>
                <td colspan={isWriter ? 4 : 3}>暂无探头</td>
              </tr>
            )}
          </tbody>
        </table>
        {isWriter && (
          <form onSubmit={onAddProbe}>
            <div class="row" style={{ marginTop: "0.75rem" }}>
              <label>
                新探头编号
                <input
                  value={newProbe.probe_id}
                  onInput={(e) => setNewProbe({ ...newProbe, probe_id: e.target.value })}
                  placeholder="例如 探头C03"
                />
              </label>
              <label>
                最近电压（V）
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  max="100"
                  value={newProbe.voltage}
                  onInput={(e) => setNewProbe({ ...newProbe, voltage: e.target.value })}
                  placeholder="例如 3.70"
                />
              </label>
              <button type="submit">登记电压</button>
            </div>
          </form>
        )}
      </div>

      <div class="card">
        <h2 class="cardtitle">监视流水</h2>
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>探头</th>
              <th>事件</th>
              <th>电压</th>
              <th>门槛</th>
              <th>说明</th>
              <th>操作人</th>
            </tr>
          </thead>
          <tbody>
            {events.map((ev) => (
              <tr key={ev.id} class={ev.event_type === "reject" ? "row-reject" : ""}>
                <td>{fmtTime(ev.created_at)}</td>
                <td>{ev.probe_id || "—"}</td>
                <td>
                  <span class={ev.event_type === "reject" ? "tag fail" : ev.event_type === "accept" ? "tag pass" : "tag wait"}>
                    {EVENT_LABELS[ev.event_type] || ev.event_type}
                  </span>
                </td>
                <td>{fmtVolt(ev.voltage)}</td>
                <td>{fmtVolt(ev.threshold)}</td>
                <td>{ev.detail}</td>
                <td>{ev.actor}</td>
              </tr>
            ))}
            {events.length === 0 && (
              <tr>
                <td colspan="7">暂无流水</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
