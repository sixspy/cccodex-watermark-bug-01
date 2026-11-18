(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  const chipEl = document.getElementById("pointer-chip");
  let tab = "pointer";
  let templates = [];
  let pointer = null;
  let batches = [];
  let reports = [];
  let jobs = [];
  let lastCorrection = null;
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    const cat = await api("/api/templates");
    templates = cat.items || [];
    pointer = cat.pointer || null;
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    jobs = (await api("/api/pointer/jobs")).items || [];
    renderPointerChip();
  }

  function renderPointerChip() {
    if (!pointer) {
      chipEl.textContent = "";
      chipEl.className = "chip";
      return;
    }
    chipEl.textContent = pointer.unique
      ? "唯一键 " + pointer.key + " = " + pointer.pointer
      : pointer.status_chip;
    chipEl.className = pointer.unique ? "chip chip-ok" : "chip chip-bad";
  }

  function pointerPanel() {
    if (!pointer) return "";
    const keys = (pointer.keys || [])
      .map(
        (k) =>
          `<li><b>${esc(k.key)}</b> = ${esc(k.value)}${
            k.key === pointer.key ? ' <span class="chip chip-ok">唯一键</span>' : ' <span class="chip chip-bad">冗余键</span>'
          }</li>`
      )
      .join("");
    return `
      <div class="pointer-card ${pointer.unique ? "ok" : "bad"}">
        <p class="eyebrow">指针目录</p>
        <h3>${esc(pointer.status_chip)}</h3>
        <p class="muted">${esc(pointer.message)}</p>
        <ul class="keys">${keys || "<li>（无指针行）</li>"}</ul>
        <div class="row">
          <label>纠偏目标指针
            <input id="ptr-input" placeholder="留空则按现有唯一列名收敛" />
          </label>
          <button type="button" id="btn-correct">点纠偏（按唯一指针一次写库）</button>
          <button type="button" class="ghost" id="btn-integrity">核唯一指针</button>
        </div>
        <p class="chip-row">脏印处置命运 <span class="chip">${esc(pointer.fate_chip)}</span></p>
      </div>
      ${
        jobs.length
          ? `<div class="cardish">
        <span class="eyebrow">编排任务落档</span>
        <table>
          <thead><tr><th>编号</th><th>指针</th><th>命运</th><th>脏印</th><th>重盖</th><th>状态</th><th>时间</th></tr></thead>
          <tbody>${jobs
            .map(
              (j) => `<tr>
            <td>${esc(j.id)}</td>
            <td>${esc(j.pointer)}</td>
            <td><span class="chip">${esc(j.fate_chip)}</span></td>
            <td>${esc(j.dirty_count)}</td>
            <td>${esc(j.restamped_count)}</td>
            <td>${esc(j.status)}</td>
            <td>${esc(j.created_at)}</td>
          </tr>`
            )
            .join("")}</tbody>
        </table>
      </div>`
          : ""
      }`;
  }

  function renderPointer() {
    const cols = (pointer && pointer.columns) || [];
    return `
      <h2>指针目录</h2>
      ${pointerPanel()}
      <h3>现行模板列名同源核</h3>
      <table>
        <thead><tr><th>表</th><th>现行列</th><th>与唯一指针同源</th></tr></thead>
        <tbody>
          ${cols
            .map(
              (c) => `<tr>
            <td>${esc(c.table_name)}</td>
            <td>${esc(c.column_name)}</td>
            <td><span class="chip ${c.aligned ? "chip-ok" : "chip-bad"}">${
                c.aligned ? "同源" : "漂移"
              }</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      ${pointerPanel()}
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th><th>指针标记</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${esc(t.version_no)}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
            <td><span class="chip ${t.pointer_aligned ? "chip-ok" : "chip-bad"}">${esc(
                t.pointer_tag
              )}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderBatches() {
    const blocked = pointer && !pointer.unique;
    return `
      <h2>盖印列表</h2>
      ${pointerPanel()}
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账</p>
      ${
        blocked
          ? '<p class="err">双指针已拦截：盖印按钮已禁用，请先在指针目录点纠偏。</p>'
          : ""
      }
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th>列名</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${esc(b.id)}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="chip">${esc(b.status)}</span></td>
            <td>${esc(b.watermark_base || "-")}${b.dirty ? ' <span class="chip chip-bad">脏印</span>' : ""}</td>
            <td>${esc(b.watermark_column || "-")}</td>
            <td>${
              b.status === "待盖印" && !blocked
                ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
                : ""
            }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    const checks = (traceResult && traceResult.column_checks) || [];
    return `
      <h2>追索详情</h2>
      ${pointerPanel()}
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <p class="muted">本页独立复验：唯一指针 ${esc(traceResult.pointer)}，列名对齐 ${
              traceResult.column_aligned ? "通过" : "未通过"
            }，脏印 ${esc(traceResult.dirty_count)} 笔。</p>
        <h3>列名同源复验</h3>
        <table>
          <thead><tr><th>表</th><th>现行列</th><th>判定</th></tr></thead>
          <tbody>${checks
            .map(
              (c) => `<tr><td>${esc(c.table_name)}</td><td>${esc(
                c.column_name
              )}</td><td><span class="chip ${
                c.aligned ? "chip-ok" : "chip-bad"
              }">${c.aligned ? "同源" : "漂移"}</span></td></tr>`
            )
            .join("")}</tbody>
        </table>
        <h3>源批次</h3>
        <ul>${src
          .map(
            (s) =>
              `<li>${esc(s.run_code)} · ${esc(s.table_name)} · ${esc(
                s.watermark_value
              )}${s.dirty ? ' <span class="chip chip-bad">脏印</span>' : ""}</li>`
          )
          .join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${
                d.watermark_value ? esc(d.watermark_value) : "未盖印"
              }</li>`
          )
          .join("")}</ul>
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    if (!pointer) return `<h2>对账水线</h2><p class="err">指针目录不可用</p>`;
    const blocked = !pointer.unique;
    return `
      <h2>对账水线</h2>
      ${pointerPanel()}
      <div class="waterline">
        <span class="eyebrow">对账水线</span>
        <span>唯一键 ${esc(pointer.key)} = ${esc(pointer.pointer)}</span>
        <span class="chip">${esc(pointer.fate_chip)}</span>
      </div>
      ${blocked ? '<p class="err">双指针即拦：缺口对账已停止，请先点纠偏。</p>' : ""}
      <button type="button" id="btn-gap" ${blocked ? "disabled" : ""}>生成对账</button>
      ${
        lastCorrection
          ? `<p class="okline">${esc(lastCorrection.message)}</p>`
          : ""
      }
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口</th><th>脏印</th><th>命运芯片</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${esc(r.id)}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${esc(r.gap_count)}</td>
            <td>${esc(r.dirty_count || 0)}</td>
            <td><span class="chip">${esc(r.fate_chip || "-")}</span></td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else if (tab === "gaps") view.innerHTML = renderGaps();
    else view.innerHTML = renderPointer();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成：列名已按唯一指针盖印");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成：列名同源复验通过");
          render();
        } catch (e) {
          traceResult = null;
          setMsg(e.message);
          render();
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const correctBtn = document.getElementById("btn-correct");
    if (correctBtn) {
      correctBtn.onclick = async () => {
        setMsg("");
        const input = document.getElementById("ptr-input");
        const target = input && input.value.trim();
        try {
          const r = await api("/api/pointer/correct", {
            method: "POST",
            body: JSON.stringify({ pointer: target || null }),
          });
          lastCorrection = r;
          await refresh();
          setMsg(r.message);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const integrityBtn = document.getElementById("btn-integrity");
    if (integrityBtn) {
      integrityBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/pointer/integrity");
          setMsg(
            (r.ok ? "复验通过：" : "复验未通过：") +
              "唯一指针 " + (r.pointer || "-") +
              "；目录与盖印读针" + (r.pointers_agree ? "一致" : "不一致") +
              "；列名 " + r.aligned_count + "/" + r.template_count + " 同源。"
          );
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      document.querySelectorAll(".dock [data-tab]").forEach((b) => {
        b.classList.toggle("on", b.dataset.tab === tab);
      });
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      render();
    })
    .catch((e) => setMsg(e.message));
})();
