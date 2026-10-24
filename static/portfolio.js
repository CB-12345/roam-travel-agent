// Portable current-trip backups and a clearly separated historical demonstration.
function updatePortfolioUI() {
  const demo = activeTrip?.mode === "recorded_demo";
  document.querySelector("#demoNotice").hidden = !demo;
  document.querySelector("#tripMore").hidden = !activeTrip;
  if (demo) {
    document.querySelector("#demoRecordedAt").textContent =
      activeTrip.recordedAt || "2026-10-24";
    document.querySelector("#openRevisionBtn").hidden = true;
    document.querySelector("#revisionPanel").hidden = true;
    closeRevisionDrawer(false);
  }
  setRevisionBusy(isPlanning);
  const trace = document.querySelector("#taskTraceLink");
  trace.hidden = !activeTrip?.lastTaskId;
  if (!trace.hidden)
    trace.href = `/api/plan/tasks/${encodeURIComponent(activeTrip.lastTaskId)}/trace`;
  renderQualitySummary(activeTrip?.plan.quality_report);
}

function renderQualitySummary(report) {
  const overview = document.querySelector("#overview");
  if (!overview) return;
  let host = document.querySelector("#qualitySummary");
  const expanded = host?.querySelector("details")?.open || false;
  if (!host) {
    host = document.createElement("div");
    host.id = "qualitySummary";
    overview.querySelector(".metric-grid")?.after(host);
    if (!host.parentNode) overview.querySelector(".plan-cover").after(host);
  }
  if (!report?.status) {
    host.innerHTML = "";
    return;
  }
  const title = report.errors
    ? `${report.errors} 项需调整`
    : report.warnings
      ? `${report.warnings} 项待确认`
      : "基础检查通过";
  const metrics = report.metrics || {};
  host.innerHTML = `<details class="quality-card" ${expanded ? "open" : ""}>
    <summary><span>行程检查</span><strong class="${report.errors ? "quality-error" : ""}">${escapeHtml(title)}</strong><svg class="icon chevron" aria-hidden="true"><use href="#i-chevron"/></svg></summary>
    <div class="quality-content"><p>地点坐标 ${Number(metrics.stops_with_coordinates || 0)}/${Number(metrics.stops || 0)} · 具体餐厅 ${Number(metrics.concrete_meals || 0)}/${Number(metrics.meals || 0)} · 开放时段吻合 ${Number(metrics.opening_fits || 0)}/${Number(metrics.stops || 0)}</p>
    ${report.issues?.length ? `<ul>${report.issues.map((issue) => `<li>${issue.day_index ? `第 ${Number(issue.day_index)} 天 · ` : ""}${escapeHtml(issue.message)}</li>`).join("")}</ul>` : "<p>日期、时间安排与费用账目未发现规则冲突。</p>"}
    <p class="mini-text">${escapeHtml(report.note)}</p></div></details>`;
}

async function loadQualityReport(plan, request) {
  if (activeTrip?.plan !== plan || activeTrip.mode === "recorded_demo") return;
  const trip = activeTrip;
  const audited = structuredClone(plan);
  for (const day of audited.daily_plans) {
    for (const item of day.items) {
      if (!isOpeningStop(item)) continue;
      const ref = findPlace(
        item.location_name || item.title,
        item.location_address,
      );
      const fact = placeDetails[ref?.key];
      if (
        fact?.verified &&
        Number.isFinite(fact.lat) &&
        Number.isFinite(fact.lng)
      )
        item.location = {
          name: fact.name,
          address: fact.address,
          lat: fact.lat,
          lng: fact.lng,
        };
      const check = openingVisitCheck(item, day.date);
      if (check) item.opening_check = check;
    }
  }
  try {
    const response = await fetch("/api/plan/quality", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request, plan: audited }),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) return;
    const report = await response.json();
    if (activeTrip !== trip || activeTrip.plan !== plan) return;
    plan.quality_report = report;
    renderQualitySummary(report);
    persistTripMetadata(trip);
  } catch {
    /* Keep the previous report without changing the itinerary. */
  }
}

async function openRecordedDemo() {
  if (isPlanning) return;
  const button = document.querySelector("#openDemoBtn");
  button.disabled = true;
  try {
    const response = await fetch("/api/demo", {
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("示例暂时无法读取，请稍后重试。");
    const backup = await response.json();
    openBackup(backup);
    setState("示例行程");
    showToast("正在查看历史示例，可查看地点、预算和导出");
  } catch (error) {
    showToast(error.message || "示例读取失败");
  } finally {
    button.disabled = false;
  }
}

function openBackup(backup) {
  activeTrip = {
    id: crypto.randomUUID(),
    createdAt: Date.now(),
    request: backup.request,
    plan: backup.plan,
    conversation: backup.conversation || [],
    dayRules: backup.day_rules || {},
    revisions: [],
    revisionCount: 0,
    placeDetails: backup.place_details || {},
    mode: backup.mode,
    recordedAt: backup.recorded_at || "",
  };
  applyRequest(activeTrip.request);
  showPage("result");
  renderPlan(activeTrip.plan, activeTrip.request);
  els.progressPanel.hidden = true;
  window.scrollTo({ top: 0, behavior: "instant" });
}

function buildTripBackup() {
  return {
    version: 1,
    request: activeTrip.request,
    plan: activeTrip.plan,
    day_rules: activeTrip.dayRules || {},
    conversation: (activeTrip.conversation || [])
      .slice(-24)
      .map(({ role, content }) => ({ role, content: content.slice(0, 1200) })),
    place_details: activeTrip.placeDetails || {},
    mode: activeTrip.mode || "personal",
    recorded_at: activeTrip.recordedAt || "",
  };
}

function exportTripBackup() {
  if (!activeTrip) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(buildTripBackup(), null, 2)], {
      type: "application/json",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `${activeTrip.plan.city.replace(/[\\/:*?"<>|]/g, "")}-${activeTrip.request.start_date}-行程备份.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  document.querySelector("#tripMore").open = false;
  showToast("备份包含当前行程、对话与锁定；不包含撤销历史");
}

async function importTripBackup(event) {
  const input = event.target,
    file = input.files?.[0];
  input.value = "";
  if (!file || isPlanning) return;
  if (file.size > 5 * 1024 * 1024) {
    showToast("请选择 5MB 以内的行程 JSON 备份");
    return;
  }
  try {
    const backup = JSON.parse((await file.text()).replace(/^\uFEFF/, ""));
    const response = await fetch("/api/trips/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(backup),
      signal: AbortSignal.timeout(10000),
    });
    const result = await response.json();
    if (!response.ok) throw { detail: result.detail };
    openBackup(result);
    saveTrip(activeTrip);
    setState("已导入");
    document.querySelector("#tripMore").open = false;
  } catch (error) {
    showToast(
      extractErrorDetail(error) || "无法导入，请选择应用导出的行程 JSON 备份",
    );
  }
}

function initializePortfolio() {
  document.querySelector("#openDemoBtn").onclick = openRecordedDemo;
  document.querySelectorAll("[data-import-trip]").forEach((button) => {
    button.onclick = () => {
      if (!isPlanning) document.querySelector("#tripImportFile").click();
    };
  });
  document.querySelector("#tripImportFile").onchange = importTripBackup;
  document.querySelector("#backupTripBtn").onclick = exportTripBackup;
}

// Metadata enrichment must never overwrite a newer itinerary from another tab.
function persistTripMetadata(trip) {
  const records = readSavedTrips();
  const stored = records.find((item) => item.id === trip.id);
  if (
    !stored ||
    (stored.updatedAt || stored.createdAt) !==
      (trip.updatedAt || trip.createdAt) ||
    stored.lastTaskId !== trip.lastTaskId
  )
    return;
  stored.placeDetails = trip.placeDetails;
  stored.plan.quality_report = trip.plan.quality_report;
  try {
    localStorage.setItem("roam.trips", JSON.stringify(records));
    savedTrips = records;
  } catch {
    /* The current in-memory trip remains readable. */
  }
}
