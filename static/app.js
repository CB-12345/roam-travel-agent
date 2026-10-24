// 旅行偏好标签配置：[后端值, 前端显示文本]
const preferenceOptions = [
  ["humanity", "人文"],
  ["art", "艺术"],
  ["nature", "自然"],
  ["food", "美食"],
  ["shopping", "购物"],
  ["nightlife", "夜生活"],
  ["family", "亲子"],
];

// 统一缓存所有DOM元素，避免重复查询，大幅提升性能
const els = {
  formPage: document.querySelector("#formPage"),
  resultPage: document.querySelector("#resultPage"),
  form: document.querySelector("#travelForm"),
  city: document.querySelector("#city"),
  startDate: document.querySelector("#startDate"),
  endDate: document.querySelector("#endDate"),
  budgetMin: document.querySelector("#budgetMin"),
  budgetMax: document.querySelector("#budgetMax"),
  extraPreferences: document.querySelector("#extraPreferences"),
  taboos: document.querySelector("#taboos"),
  travelers: document.querySelector("#travelers"),
  pace: document.querySelector("#pace"),
  hotelStyle: document.querySelector("#hotelStyle"),
  transportMode: document.querySelector("#transportMode"),
  transitPreferenceWrap: document.querySelector("#transitPreferenceWrap"),
  transitPreference: document.querySelector("#transitPreference"),
  preferenceChips: document.querySelector("#preferenceChips"),
  healthText: document.querySelector("#healthText"),
  runState: document.querySelector("#runState"),
  sourceBar: document.querySelector("#sourceBar"),
  progressPanel: document.querySelector("#progressPanel"),
  progressTitle: document.querySelector("#progressTitle"),
  progressPercent: document.querySelector("#progressPercent"),
  progressFill: document.querySelector("#progressFill"),
  progressDetail: document.querySelector("#progressDetail"),
  resultView: document.querySelector("#resultView"),
  resultNav: document.querySelector("#resultNav"),
  submitBtn: document.querySelector("#submitBtn"),
  backToFormBtn: document.querySelector("#backToFormBtn"),
};

// 存储用户选中的偏好标签，使用Set结构方便快速增删和判断存在性
const selectedPreferences = new Set(["humanity", "food"]);
const MAX_TRIP_DAYS = 7;
const LONG_TRIP_MESSAGE =
  "本工具适合 1-7 天轻量旅行建议，请把行程控制在 7 天以内。";
// SSE(服务器推送事件)连接对象，用于接收后端实时进度
let progressSource = null;
// 当前进度百分比，只会递增，保证用户体验流畅
let currentProgressPercent = 0;
let isPlanning = false;
let activeTrip = null;
let savedTrips = readSavedTrips();
let progressHideTimer = null;
let toastTimer = null;

// 规划过程的8个阶段，对应进度条的不同节点
const progressStages = [
  {
    percent: 8,
    title: "校验旅行需求",
    detail: "正在检查城市、日期、预算和出行方式。",
  },
  {
    percent: 18,
    title: "景点 Agent 规划搜索",
    detail: "AttractionSearchAgent 正在理解偏好和忌讳，并调用高德 POI 工具。",
  },
  {
    percent: 30,
    title: "天气 Agent 研究",
    detail: "WeatherSearchAgent 正在整理天气风险和出行建议。",
  },
  {
    percent: 42,
    title: "酒店 Agent 筛选",
    detail: "HotelSearchAgent 正在按住宿节奏和区域生成酒店候选。",
  },
  {
    percent: 55,
    title: "餐饮与预算交给最终 Agent",
    detail: "最终行程 Agent 将直接生成餐饮安排和价格估算。",
  },
  {
    percent: 68,
    title: "行程 Agent 编排",
    detail:
      "ItineraryPlanningAgent 正在把景点、酒店、餐饮、价格和天气合成每日行程。",
  },
  {
    percent: 82,
    title: "高德路线规划",
    detail: "正在为每天的地点链计算公交、步行、打车或自驾路线。",
  },
  {
    percent: 93,
    title: "整理可视化报告",
    detail: "正在汇总预算、风险提醒、搜索计划和详细路径。",
  },
];

function showPage(page) {
  closeRevisionDrawer(false);
  els.formPage.classList.toggle("page-active", page === "form");
  els.resultPage.classList.toggle("page-active", page === "result");
  document.querySelector("#workspaceTitle").textContent =
    page === "form" ? "新的旅行" : `${els.city.value || "专属"}行程`;
  closeSidebar();
}

function renderPreferenceChips() {
  els.preferenceChips.innerHTML = "";
  preferenceOptions.forEach(([value, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `chip ${selectedPreferences.has(value) ? "active" : ""}`;
    button.textContent = label;
    button.setAttribute("aria-pressed", String(selectedPreferences.has(value)));
    button.addEventListener("click", () => {
      if (selectedPreferences.has(value)) {
        selectedPreferences.delete(value);
      } else {
        selectedPreferences.add(value);
      }
      renderPreferenceChips();
    });
    els.preferenceChips.appendChild(button);
  });
}

// 根据出行方式控制公交偏好选项的显示/隐藏
function updateTransitPreferenceVisibility() {
  els.transitPreferenceWrap.style.display =
    els.transportMode.value === "public_transit" ? "grid" : "none";
}

async function loadHealth() {
  const response = await fetch("/api/health");
  if (!response.ok) throw new Error("服务连接失败");
  const data = await response.json();
  els.healthText.textContent = data.llm_enabled ? "服务已就绪" : "模型尚未配置";
  els.healthText.classList.toggle("ready", Boolean(data.llm_enabled));
  els.healthText.title = data.llm_enabled
    ? `规划模型：${data.provider}`
    : "请在本地配置中启用模型服务";
}

function setState(text) {
  els.runState.textContent = text;
}

// 更新进度条的显示
function setProgress(stage) {
  const percent = Number(stage.percent || 0);
  currentProgressPercent = Math.max(currentProgressPercent, percent);
  els.progressPanel.hidden = false;
  const presentation = progressPresentation(stage);
  els.progressTitle.textContent = presentation.title;
  els.progressDetail.textContent = presentation.detail;
  els.progressPercent.textContent = `${currentProgressPercent}%`;
  els.progressFill.style.width = `${currentProgressPercent}%`;
  document.querySelectorAll("#progressSteps [data-step]").forEach((step) => {
    const index = Number(step.dataset.step);
    step.classList.toggle(
      "done",
      currentProgressPercent === 100 || index < presentation.step,
    );
    step.classList.toggle(
      "current",
      currentProgressPercent < 100 && index === presentation.step,
    );
  });
}

function startProgress() {
  clearTimeout(progressHideTimer);
  stopProgress();
  currentProgressPercent = 0;
  setProgress({
    percent: 3,
    title: "任务已提交",
    detail: "正在连接后端真实进度通道。",
  });
}

function completeProgress() {
  stopProgress();
  currentProgressPercent = 100;
  setProgress({
    percent: 100,
    title: "旅行计划生成完成",
    detail: "报告、预算、路线和风险提醒已经整理好。",
  });
  progressHideTimer = window.setTimeout(() => {
    els.progressPanel.hidden = true;
  }, 700);
}

function failProgress() {
  stopProgress();
  setProgress({
    percent: Math.max(12, currentProgressPercent || 12),
    title: "生成过程遇到问题",
    detail: "请查看下方错误提示并返回编辑。",
  });
}

function stopProgress() {
  if (progressSource) {
    progressSource.close();
    progressSource = null;
  }
}

function createPlanTask(payload) {
  return postBackgroundTask("plan", payload);
}

function waitForPlanStream(taskId, options = {}) {
  return watchBackgroundTask(taskId, options);
}

function parseSsePayload(raw) {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return { detail: raw };
  }
}

// 从表单中收集数据，构建后端需要的请求格式
function buildPayload() {
  return {
    city: els.city.value.trim(),
    start_date: els.startDate.value,
    end_date: els.endDate.value,
    budget_min: Number(els.budgetMin.value),
    budget_max: Number(els.budgetMax.value),
    preferences: [...selectedPreferences],
    extra_preferences: els.extraPreferences.value.trim(),
    taboos: els.taboos.value.trim(),
    travelers: Number(els.travelers.value),
    pace: els.pace.value,
    hotel_style: els.hotelStyle.value,
    transport_mode: els.transportMode.value,
    transit_preference: els.transitPreference.value,
    departure_city: "",
  };
}

function getTripDayCount(startValue, endValue) {
  if (!startValue || !endValue) {
    return 0;
  }
  const start = new Date(`${startValue}T00:00:00`);
  const end = new Date(`${endValue}T00:00:00`);
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) {
    return 0;
  }
  return Math.floor((end - start) / 86400000) + 1;
}

function updateDateSpanValidity({ report = false } = {}) {
  const dayCount = getTripDayCount(els.startDate.value, els.endDate.value);
  const isTooLong = dayCount > MAX_TRIP_DAYS;
  els.endDate.setCustomValidity(isTooLong ? LONG_TRIP_MESSAGE : "");
  els.submitBtn.disabled = isPlanning || isTooLong;
  const error = document.querySelector("#formError");
  error.hidden = !isTooLong;
  error.textContent = isTooLong ? LONG_TRIP_MESSAGE : "";
  if (report && isTooLong) {
    els.endDate.reportValidity();
  }
  return isTooLong ? { detail: LONG_TRIP_MESSAGE, focus: els.endDate } : null;
}

// 前端验证表单数据的合法性
function validatePayload(payload) {
  if (!payload.city) {
    return { detail: "请输入旅行城市。", focus: els.city };
  }
  if (
    payload.start_date &&
    payload.end_date &&
    payload.end_date < payload.start_date
  ) {
    return { detail: "结束日期必须大于等于开始日期", focus: els.endDate };
  }
  const longTripError = updateDateSpanValidity();
  if (longTripError) {
    return longTripError;
  }
  if (
    Number.isFinite(payload.budget_min) &&
    Number.isFinite(payload.budget_max) &&
    payload.budget_max < payload.budget_min
  ) {
    return { detail: "最高预算必须大于等于最低预算", focus: els.budgetMax };
  }
  return null;
}

// 转义HTML特殊字符，防止XSS跨站脚本攻击
function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function normalizeAddress(value) {
  return String(value || "").trim();
}

function timeSortKey(value) {
  const text = String(value || "").trim();
  const start = text.split("-", 1)[0].trim();
  const hourText = start.split(":", 1)[0].trim();
  const hour = Number.parseInt(hourText, 10);
  if (Number.isFinite(hour) && hour >= 0 && hour <= 23) {
    return [hour, text];
  }
  return [99, text];
}

function sortItemsByTime(items) {
  return [...items].sort((left, right) => {
    const [leftHour, leftText] = timeSortKey(left?.time_range);
    const [rightHour, rightText] = timeSortKey(right?.time_range);
    return (
      leftHour - rightHour || leftText.localeCompare(rightText, "zh-Hans-CN")
    );
  });
}

function buildLocationAddressMap(plan) {
  const addressMap = {};

  // 辅助函数：向映射表中添加地址
  const add = (name, address) => {
    const key = String(name || "").trim();
    const value = normalizeAddress(address);
    if (key && value && !addressMap[key]) {
      addressMap[key] = value;
    }
  };

  // 1. 从选中的景点中提取地址
  (plan.selected_attractions || []).forEach((item) => {
    add(item.name, item.location?.address);
    add(item.location?.name, item.location?.address);
  });

  // 2. 从推荐酒店中提取地址
  if (plan.recommended_hotel) {
    add(plan.recommended_hotel.name, plan.recommended_hotel.location?.address);
    add(
      plan.recommended_hotel.location?.name,
      plan.recommended_hotel.location?.address,
    );
  }
  (plan.hotel_candidates || []).forEach((item) => {
    add(item.name, item.location?.address);
    add(item.location?.name, item.location?.address);
  });
  (plan.daily_stays || []).forEach((stay) => {
    [stay.start_hotel, stay.end_hotel].forEach((hotel) => {
      if (!hotel) return;
      add(hotel.name, hotel.location?.address);
      add(hotel.location?.name, hotel.location?.address);
    });
  });

  // 3. 从每日行程中提取地址
  (plan.daily_plans || []).forEach((day) => {
    (day.items || []).forEach((item) => {
      add(item.location_name, item.location_address);
      add(item.title, item.location_address);
      add(item.to_location, item.location_address);
    });
  });

  return addressMap;
}

function resolveItemAddress(item, addressMap) {
  return (
    normalizeAddress(item.location_address) ||
    addressMap[item.location_name] ||
    addressMap[item.title] ||
    addressMap[item.to_location] ||
    ""
  );
}

function transportModeLabel(value) {
  return (
    {
      public_transit: "公共交通",
      self_drive: "自驾游",
      taxi: "打车",
      mixed: "混合出行",
    }[value] || value
  );
}

function transitPreferenceLabel(value) {
  return (
    {
      recommended: "推荐",
      less_walking: "步行时间少",
      subway_priority: "地铁优先",
      bus_priority: "公交优先",
    }[value] || value
  );
}

function routeDataModeLabel(value) {
  return (
    {
      live: "实时高德",
      live_outlier: "高德实时结果异常",
      live_walk: "实时高德步行",
      fallback: "本地估算",
    }[value] ||
    value ||
    "未知"
  );
}

function routeFallbackReasonLabel(value) {
  if (!value) return "";
  if (
    value.includes("10021") ||
    value.includes("CUQPS_HAS_EXCEEDED_THE_LIMIT")
  ) {
    return "高德公交接口限流，已回退本地估算";
  }
  if (value.includes("origin_poi_geocode_failed")) {
    return "起点 POI 识别失败，已回退本地估算";
  }
  if (value.includes("destination_poi_geocode_failed")) {
    return "终点 POI 识别失败，已回退本地估算";
  }
  if (value.includes("amap_transit_empty")) {
    if (value.includes("short_distance_walk_api")) {
      return "高德未返回公交方案，因距离较短已改查实时步行路线";
    }
    return "高德未返回可用公交方案，当前只展示本地估算，不代表真实线路";
  }
  if (
    value.includes("route_distance_outlier") ||
    value.includes("route_duration_outlier")
  ) {
    return "路线距离或时间异常，可能存在地点匹配偏差，已保留你选择的出行方式";
  }
  if (value.includes("transit_request_exception")) {
    return "公交路径请求异常，已回退本地估算";
  }
  if (value.includes("driving_request_exception")) {
    return "驾车路径请求异常，已回退本地估算";
  }
  if (value.includes("live_transport_unavailable")) {
    return "实时交通结果不可用，已回退本地估算";
  }
  return value;
}

function segmentTypeLabel(value) {
  return (
    {
      walk: "步行",
      subway: "地铁",
      railway: "铁路/城际",
      bus: "公交",
      transit: "公共交通",
      taxi: "打车",
      drive: "自驾",
      walk: "步行",
    }[value] || value
  );
}

function renderTransitLineMeta(segment) {
  const parts = [];
  if (segment.line_name) parts.push(escapeHtml(segment.line_name));
  if (segment.direction) parts.push(escapeHtml(segment.direction));
  return parts.length ? parts.join(" · ") : "";
}

function renderTransitStationMeta(segment) {
  const parts = [];
  if (segment.on_station || segment.off_station) {
    parts.push(
      `上车：${escapeHtml(segment.on_station || "-")} · 下车：${escapeHtml(segment.off_station || "-")}`,
    );
  }
  if (segment.entrance || segment.exit) {
    parts.push(
      `入口：${escapeHtml(segment.entrance || "-")} · 出口：${escapeHtml(segment.exit || "-")}`,
    );
  }
  if (segment.via_stops && segment.via_stops.length) {
    parts.push(`途经：${escapeHtml(segment.via_stops.join("、"))}`);
  }
  const alternatives =
    segment.details && Array.isArray(segment.details.alternatives)
      ? segment.details.alternatives
      : [];
  if (alternatives.length) {
    const lines = alternatives
      .map((item) => item.line_name || "")
      .filter(Boolean)
      .join("、");
    if (lines) {
      parts.push(`可选线路：${escapeHtml(lines)}`);
    }
  }
  return parts
    .map((item) => `<div class="route-segment-meta">${item}</div>`)
    .join("");
}

function renderSearchPlan(plan) {
  const searchPlan = Array.isArray(plan.attraction_search_plan)
    ? plan.attraction_search_plan
    : [];
  const interpretation = plan.preference_interpretation || {};
  const positive = [
    ...(Array.isArray(interpretation.positive) ? interpretation.positive : []),
    ...(Array.isArray(interpretation.extra_positive)
      ? interpretation.extra_positive
      : []),
  ].filter(Boolean);
  const negative = Array.isArray(interpretation.negative)
    ? interpretation.negative.filter(Boolean)
    : [];

  if (!searchPlan.length && !positive.length && !negative.length) {
    return "";
  }

  return `
    <div class="agent-plan-card">
      <div class="mini-title">Agent 搜索计划</div>
      ${positive.length ? `<div class="mini-text">正向偏好：${escapeHtml(positive.join("、"))}</div>` : ""}
      ${negative.length ? `<div class="mini-text">负向约束：${escapeHtml(negative.join("、"))}</div>` : ""}
      ${
        searchPlan.length
          ? `
        <div class="search-query-list">
          ${searchPlan
            .map(
              (item) => `
            <div class="search-query">
              <strong>${escapeHtml(item.query || item.theme || "未命名搜索")}</strong>
              <span>${escapeHtml(item.reason || "")}</span>
            </div>
          `,
            )
            .join("")}
        </div>
      `
          : ""
      }
    </div>
  `;
}

// 渲染AI Agent的运行诊断信息，用于调试和透明化
function diagnosticReasonLabel(value) {
  return (
    {
      llm_output_json_parse_failed: "模型没有返回可解析 JSON",
      attraction_research_schema_validation_failed:
        "模型 JSON 不符合景点研究结构",
      selected_attractions_not_list: "selected_attractions 不是列表",
      agent_selected_no_attractions: "Agent 没有选出景点",
      mcp_candidate_pool_empty: "MCP 候选池为空",
      no_agent_attractions_matched_candidate_pool:
        "Agent 景点未匹配到 MCP 候选池",
      agent_grounding_failed: "Agent 输出未通过候选池验真",
      attraction_agent_postprocess_exception: "Agent 后处理异常",
      attraction_agent_unavailable_or_unverified:
        "Agent 不可用或结果未通过验真",
    }[value] ||
    value ||
    "无"
  );
}

function renderAgentDiagnostics(plan) {
  const diagnostics = plan.agent_diagnostics || {};
  if (!diagnostics || !Object.keys(diagnostics).length) {
    return "";
  }
  const isFallback = Boolean(diagnostics.fallback_used);
  const statusText = isFallback
    ? "本轮已启用备用景点检索"
    : diagnostics.grounding_ok
      ? "景点 Agent 已通过候选池验真"
      : "景点 Agent 运行诊断";
  const rawPreview = String(diagnostics.raw_output_preview || "").trim();
  const removed = Array.isArray(diagnostics.removed_unverifiable)
    ? diagnostics.removed_unverifiable
    : [];
  const groundingRemoved = Array.isArray(diagnostics.grounding_removed)
    ? diagnostics.grounding_removed
    : [];
  return `
    <div class="agent-diagnostic-card ${isFallback ? "is-fallback" : "is-ok"}">
      <div class="agent-diagnostic-head">
        <div>
          <div class="mini-title">${escapeHtml(statusText)}</div>
          <div class="mini-text">原因：${escapeHtml(diagnosticReasonLabel(diagnostics.failure_reason))}</div>
        </div>
        <span class="section-tag">${escapeHtml(diagnostics.provider || "Agent")}</span>
      </div>
      <div class="diagnostic-grid">
        <div><span>JSON 解析</span><strong>${diagnostics.json_parse_ok ? "成功" : "失败"}</strong></div>
        <div><span>原始输出</span><strong>${Number(diagnostics.raw_output_length || 0)} 字符</strong></div>
        <div><span>模型原选</span><strong>${Number(diagnostics.raw_selected_count || 0)} 个</strong></div>
        <div><span>字段验真</span><strong>${Number(diagnostics.verified_selected_count || 0)} 个</strong></div>
        <div><span>MCP 候选池</span><strong>${Number(diagnostics.candidate_pool_size || 0)} 个</strong></div>
        <div><span>候选池匹配</span><strong>${Number(diagnostics.grounded_selected_count || 0)} 个</strong></div>
      </div>
      ${
        removed.length
          ? `
        <div class="diagnostic-note">
          字段缺失被移除：${escapeHtml(removed.map((item) => `${item.name || "未命名"}(${(item.missing || []).join("/")})`).join("、"))}
        </div>
      `
          : ""
      }
      ${
        groundingRemoved.length
          ? `
        <div class="diagnostic-note">
          候选池未匹配：${escapeHtml(groundingRemoved.join("、"))}
        </div>
      `
          : ""
      }
      ${
        rawPreview
          ? `
        <details class="diagnostic-raw">
          <summary>查看模型原始输出预览</summary>
          <div>${escapeHtml(rawPreview)}</div>
        </details>
      `
          : ""
      }
    </div>
  `;
}

function renderRouteSegments(item) {
  if (!item.route_segments || !item.route_segments.length) {
    return "";
  }
  const alternatives = Array.isArray(item.route_alternatives)
    ? item.route_alternatives
    : [];
  return `
    <details class="route-details">
      <summary>查看详细路径</summary>
      ${item.route_data_mode ? `<div class="route-note">来源：${escapeHtml(routeDataModeLabel(item.route_data_mode))}</div>` : ""}
      ${item.route_strategy ? `<div class="route-note">策略：${escapeHtml(item.route_strategy)}</div>` : ""}
      ${item.route_fallback_reason ? `<div class="route-note">回退原因：${escapeHtml(routeFallbackReasonLabel(item.route_fallback_reason))}</div>` : ""}
      ${renderRouteSegmentList(item.route_segments)}
      ${renderRouteAlternatives(alternatives)}
    </details>
  `;
}

function renderRouteAlternatives(alternatives) {
  if (!alternatives.length) {
    return "";
  }
  return `
    <div class="route-alternatives">
      <div class="route-alternatives-title">备选方案</div>
      ${alternatives
        .map(
          (alt, index) => `
        <details class="route-alt-card">
          <summary>
            <span>方案 ${index + 1}</span>
            <strong>${escapeHtml((alt.lines || []).join(" + ") || "公共交通方案")}</strong>
            <em>${Number(alt.duration_min || 0)}分钟 · ${Number(alt.distance_km || 0).toFixed(1)}公里 · ¥${Number(alt.cost || 0).toFixed(0)}</em>
          </summary>
          <div class="route-alt-meta">
            步行 ${Number(alt.walk_distance_m || 0).toFixed(0)} 米 · 换乘 ${Number(alt.transfers || 0).toFixed(0)} 次
          </div>
          ${renderRouteSegmentList(alt.route_segments || [])}
        </details>
      `,
        )
        .join("")}
    </div>
  `;
}

function renderRouteSegmentList(segments) {
  if (!segments || !segments.length) {
    return `<div class="route-note">该方案没有返回可展开的站点明细。</div>`;
  }
  return `
      <div class="route-segment-list">
        ${segments
          .map(
            (segment, idx) => `
          <div class="route-segment">
            <div class="route-segment-head">${idx + 1}. ${escapeHtml(segmentTypeLabel(segment.segment_type))}</div>
            <div class="route-segment-text">${escapeHtml(segment.instruction || "无详细说明")}</div>
            <div class="route-segment-meta">
              ${segment.duration_min ? `${segment.duration_min} 分钟` : ""}
              ${segment.distance_m ? ` · ${segment.distance_m} 米` : ""}
              ${renderTransitLineMeta(segment) ? ` · ${renderTransitLineMeta(segment)}` : ""}
            </div>
            ${renderTransitStationMeta(segment)}
          </div>
        `,
          )
          .join("")}
      </div>
  `;
}

function renderPlanItem(item, addressMap = {}, dayIndex = 0) {
  if (["transport", "hotel"].includes(item.item_type)) return "";
  const address = resolveItemAddress(item, addressMap);
  const name = item.location_name || item.title;
  const label =
    {
      attraction: "游玩",
      meal:
        { breakfast: "早餐", lunch: "午餐", dinner: "晚餐" }[item.meal_type] ||
        "用餐",
      leisure: "自由活动",
    }[item.item_type] || "停留";
  const attraction = activeTrip?.plan.selected_attractions.find(
    (p) => p.name === name,
  );
  return `<article class="stop-card">
    <div class="stop-time"><span class="timeline-dot" aria-hidden="true"></span><strong>${escapeHtml(item.time_range || "时间灵活")}</strong><span>${label}</span></div>
    <div class="stop-content"><div class="stop-copy"><div class="stop-title-row"><h4>${escapeHtml(name)}</h4><span class="stop-cost">预计 ¥${Math.round(Number(item.estimated_cost || 0))}</span></div>
      ${item.title && item.title !== name ? `<p class="stop-subtitle">${escapeHtml(item.title)}</p>` : ""}
      <p class="stop-description">${escapeHtml(item.summary || "")}</p>
      ${attraction?.recommended_hours ? `<span class="stop-duration">建议停留 ${Number(attraction.recommended_hours)} 小时</span>` : ""}
      ${renderPlaceAddress(name, address)}${renderPlaceMap(name, address)}
      ${renderOpeningHours(item, dayIndex, address)}
      ${renderStopActions(item.item_type === "food" ? "meal" : item.item_type, dayIndex, name, item.location_address || "", item.meal_type || "", item.is_route_stop !== false)}
    </div>${renderPlacePhoto(name, address, true)}</div>
  </article>`;
}

function sourceLabel(value) {
  if (value === "recorded_demo") return "历史录制示例";
  const labels = {
    llm_generated: "智能规划",
    program_fallback: "规则规划",
    live_amap: "高德实时",
    live_qweather: "和风天气预报",
    partial_qweather: "和风预报（部分日期）",
    unavailable: "暂无预报",
    live_poi_estimated_price: "高德POI+价格估算",
    local_sample: "样例数据",
    fallback: "样例或估算",
    agent_generated: "模型整理",
  };
  return labels[value] || value || "未知";
}

function renderSources(plan) {
  els.sourceBar.innerHTML = `
    <div class="source-chip">规划来源：${escapeHtml(sourceLabel(plan.planning_source))}</div>
    <div class="source-chip">景点数据：${escapeHtml(sourceLabel(plan.attraction_data_source))}</div>
    <div class="source-chip">天气数据：${escapeHtml(sourceLabel(plan.weather_data_source))}${["live_qweather", "partial_qweather"].includes(plan.weather_data_source) ? ' · <a href="https://www.qweather.com/" target="_blank" rel="noopener noreferrer">和风天气</a>' : ""}</div>
    <div class="source-chip">酒店数据：${escapeHtml(sourceLabel(plan.hotel_data_source))}</div>
  `;
}

function extractErrorDetail(errorLike) {
  if (typeof errorLike === "string") {
    return errorLike;
  }
  const detail = errorLike?.detail;
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0];
    const message = String(first?.msg || "输入参数校验失败").trim();
    const loc = Array.isArray(first?.loc) ? first.loc.join(" / ") : "";
    return loc ? `${message}（${loc}）` : message;
  }
  if (typeof errorLike?.message === "string" && errorLike.message.trim()) {
    return errorLike.message.trim();
  }
  return "生成失败";
}

function buildFriendlyError(errorLike) {
  const detail = extractErrorDetail(errorLike);
  const normalizedDetail = detail.toLowerCase().replace(/\s+/g, "");
  let title = "暂时无法生成这份行程";
  let hint = "请检查输入信息后重试。";

  if (detail.includes("请输入旅行城市")) {
    title = "还没有填写旅行城市";
    hint = "先输入一个清晰的中文或英文城市名，再继续生成行程。";
  } else if (isDateRangeError(normalizedDetail)) {
    title = "日期范围不正确";
    hint = "结束日期需要晚于或等于开始日期，请调整日期后再试。";
  } else if (isBudgetRangeError(normalizedDetail)) {
    title = "预算范围不正确";
    hint = "最高预算需要大于或等于最低预算，请调整预算区间。";
  } else if (detail.includes("value_error")) {
    title = "输入信息还不完整";
    hint = "请检查日期、预算、人数和城市是否填写正确。";
  } else if (detail.includes("城市名称编码异常")) {
    title = "城市名称没有识别成功";
    hint = "请重新输入清晰的中文或英文城市名，例如：广州、汕头、Beijing。";
  } else if (detail.includes("暂不支持城市")) {
    title = "当前城市暂不支持自动规划";
    hint = "可以换一个已知城市，或者换成地图服务能够识别的标准城市名再试。";
  } else if (detail.includes("暂无可用景点数据")) {
    title = "景点数据暂时没有准备好";
    hint = "这个城市可能缺少可用景点结果，建议换个写法，或稍后重试。";
  } else if (detail.includes("暂无可用酒店数据")) {
    title = "酒店数据暂时没有准备好";
    hint = "可以保留城市不变，稍后重试，或换一个更常见的城市写法。";
  } else if (detail.includes("暂无可用交通参考数据")) {
    title = "交通路线暂时没算出来";
    hint =
      "这通常是地图服务没有稳定返回路线结果。可以稍后重试，或改成中文城市名再试一次。";
  }

  return { title, detail, hint };
}

function isDateRangeError(normalizedDetail) {
  const patterns = [
    "end_datemustbegreaterthanorequaltostart_date",
    "结束日期必须大于或等于开始日期",
    "结束日期必须大于等于开始日期",
    "结束日期需要晚于或等于开始日期",
  ];
  return patterns.some((pattern) =>
    normalizedDetail.includes(pattern.toLowerCase().replace(/\s+/g, "")),
  );
}

function isBudgetRangeError(normalizedDetail) {
  const patterns = [
    "budget_maxmustbegreaterthanorequaltobudget_min",
    "最高预算必须大于或等于最低预算",
    "最高预算必须大于等于最低预算",
    "最高预算需要大于或等于最低预算",
    "大预算值必须大于或等于最小预算值",
  ];
  return patterns.some((pattern) =>
    normalizedDetail.includes(pattern.toLowerCase().replace(/\s+/g, "")),
  );
}

function resetResultView() {
  document.querySelector("#demoNotice").hidden = true;
  document.querySelector("#tripMore").hidden = true;
  document.querySelector("#taskTraceLink").hidden = true;
  document.querySelector("#revisionPanel").hidden = true;
  document.querySelector("#openRevisionBtn").hidden = true;
  document.querySelector("#revisionSummary").hidden = true;
  document
    .querySelector("#resultWorkbench")
    .classList.remove("with-conversation");
  document.querySelector(".result-shell").classList.remove("has-conversation");
  document.querySelector("#sourceDetails").hidden = true;
  document.querySelector("#exportBtn").hidden = true;
  if (els.resultNav) {
    els.resultNav.hidden = true;
  }
  els.sourceBar.innerHTML = `
    <div class="source-chip">规划来源：未生成</div>
    <div class="source-chip">景点数据：未生成</div>
    <div class="source-chip">天气数据：未生成</div>
    <div class="source-chip">酒店数据：未生成</div>
  `;
  els.resultView.className = "result-view empty";
  els.resultView.innerHTML = `
    <div class="planning-placeholder"><svg class="icon" aria-hidden="true"><use href="#i-map"/></svg><h2>好行程，值得一点等待</h2><p>正在为你寻找值得停留的地方。</p><div class="skeleton-line"></div><div class="skeleton-line short"></div></div>
  `;
}

function bindErrorActions() {
  document.querySelector("#errorResetBtn")?.addEventListener("click", () => {
    resetResultView();
    setState("等待输入");
  });
  document.querySelector("#errorFocusBtn")?.addEventListener("click", () => {
    showPage("form");
    els.city.focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

function renderErrorCard(errorLike) {
  const friendly = buildFriendlyError(errorLike);
  if (els.resultNav) {
    els.resultNav.hidden = true;
  }
  els.sourceBar.innerHTML = `
    <div class="source-chip">规划来源：未生成</div>
    <div class="source-chip">状态：输入或数据需要调整</div>
  `;
  els.resultView.className = "result-view";
  els.resultView.innerHTML = `
    <section class="error-card">
      <div class="error-badge">本次未生成成功</div>
      <h2>${escapeHtml(friendly.title)}</h2>
      <p class="error-detail">${escapeHtml(friendly.detail)}</p>
      <div class="error-hint">
        <div class="error-hint-title">建议</div>
        <div>${escapeHtml(friendly.hint)}</div>
      </div>
      <div class="error-actions">
        <button type="button" class="secondary-button" id="errorResetBtn">清空结果</button>
        <button type="button" class="ghost-button" id="errorFocusBtn">返回编辑</button>
      </div>
    </section>
  `;
  bindErrorActions();
}

function collectRouteStops(plan) {
  const stops = [];
  (plan.daily_plans || []).forEach((day, dayIndex) => {
    sortItemsByTime(day.items || []).forEach((item) => {
      if (item.item_type === "transport") return;
      const name = item.location_name || item.title;
      if (!name) return;
      stops.push({
        day: dayIndex + 1,
        time: item.time_range || "",
        name,
        type: item.item_type || "stop",
      });
    });
  });
  return stops;
}

function renderRouteMap(plan) {
  const seen = new Set();
  const locations = (plan.selected_attractions || [])
    .map((attraction) => ({
      name: attraction.name,
      lat: Number(attraction.location?.lat),
      lng: Number(attraction.location?.lng),
    }))
    .filter((location) => {
      if (
        !Number.isFinite(location.lat) ||
        !Number.isFinite(location.lng) ||
        !location.lat ||
        !location.lng ||
        seen.has(location.name)
      )
        return false;
      seen.add(location.name);
      return true;
    })
    .slice(0, 6);
  let diagram =
    '<div class="route-empty">地点坐标暂不可用<br>可在每日安排中查看交通建议</div>';
  if (locations.length) {
    const lngs = locations.map((p) => p.lng);
    const lats = locations.map((p) => p.lat);
    const minLng = Math.min(...lngs),
      maxLng = Math.max(...lngs);
    const minLat = Math.min(...lats),
      maxLat = Math.max(...lats);
    const points = locations.map((p) => ({
      ...p,
      x:
        maxLng === minLng
          ? 190
          : 55 + ((p.lng - minLng) / (maxLng - minLng)) * 270,
      y:
        maxLat === minLat
          ? 112
          : 35 + ((maxLat - p.lat) / (maxLat - minLat)) * 160,
    }));
    diagram = `<svg class="route-svg" viewBox="0 0 390 240" role="img" aria-label="景点位置示意，非导航路线">
      ${points.map((p, i) => `<circle cx="${p.x}" cy="${p.y}" r="13" fill="var(--accent-soft)" stroke="var(--primary)" stroke-width="1"/><text x="${p.x}" y="${p.y + 3.5}" text-anchor="middle">${i + 1}</text><text class="map-label-halo" x="${p.x}" y="${p.y + 29}" text-anchor="middle">${escapeHtml(p.name.length > 10 ? p.name.slice(0, 9) + "…" : p.name)}</text>`).join("")}
    </svg>`;
  }
  return `<section class="report-section" id="route-map"><div class="section-title-row"><h2>目的地一览</h2><span class="section-tag">景点位置示意</span></div>
    <div class="route-map-panel"><div class="route-canvas">${diagram}</div><div class="route-map-side"><h3>${escapeHtml(plan.city)} · 值得停留的地方</h3>
    ${locations.map((p, i) => `<div class="route-stop-mini"><span class="route-stop-index">${i + 1}</span>${escapeHtml(p.name)}</div>`).join("")}
    <p class="route-map-caption">按景点坐标绘制相对位置，不代表导航路线。<br>游玩顺序和交通建议请查看每日安排。</p></div></div></section>`;
}

function hotelPriceLabel(hotel) {
  const low = Number(hotel?.nightly_price_min);
  const high = Number(hotel?.nightly_price_max);
  if (low > 0 && high >= low)
    return `参考 ¥${Math.round(low)}–${Math.round(high)}`;
  return `约 ¥${Math.round(Number(hotel?.nightly_price || 0))}`;
}

function renderHotelCard(hotel, title = "") {
  if (!hotel) return "<p class='mini-text'>暂无酒店推荐</p>";
  const address = normalizeAddress(hotel.location?.address);
  return `<article class="stay-place-card">${renderPlacePhoto(hotel.name, address)}<div class="stay-place-copy">
    ${title ? `<p class="hotel-card-kicker">${escapeHtml(title)}</p>` : ""}<h3>${escapeHtml(hotel.name)}</h3>
    <p class="stay-price">${hotelPriceLabel(hotel)}<span> / 间夜</span></p>
    ${renderPlaceAddress(hotel.name, address)}
    <p class="stay-price-note">按酒店类别预留的参考预算，非实时报价；实际房价与房态请向酒店或预订平台确认。</p><div class="place-actions">${renderPlaceMap(hotel.name, address)}</div>${renderHotelActions(hotel)}
  </div></article>`;
}

function renderDailyHotelCard(stay, dayIndex, hotelItem = null) {
  const hotel = stay?.end_hotel || stay?.start_hotel;
  if (!hotel && !hotelItem) return "";
  const name = hotel?.name || hotelItem.location_name || hotelItem.title;
  const address = hotel?.location?.address || hotelItem?.location_address || "";
  const price = stay?.charged_night
    ? `${hotelPriceLabel(hotel)} / 间夜`
    : "当日不新增住宿费";
  return `<section class="day-stay"><div class="day-stay-heading"><span>当日住宿</span><strong>${escapeHtml(name)}</strong></div><div class="day-stay-body"><p>${escapeHtml(price)}${stay?.hotel_changed ? " · 当日换宿" : ""}</p>${renderPlaceAddress(name, address)}${renderPlaceMap(name, address)}${renderHotelActions(hotel, dayIndex + 1)}</div></section>`;
}

function getDailyStayForDay(plan, dayIndex) {
  const stays = Array.isArray(plan.daily_stays) ? plan.daily_stays : [];
  const expectedDay = dayIndex + 1;
  return (
    stays.find((stay) => Number(stay?.day_index) === expectedDay) ||
    stays[dayIndex] ||
    null
  );
}

function getDayHotelItem(day) {
  const items = Array.isArray(day?.items) ? day.items : [];
  return items.find((item) => item?.item_type === "hotel") || null;
}

function getVisibleDayItems(day) {
  const items = Array.isArray(day?.items) ? day.items : [];
  return items.filter(
    (item) => !["hotel", "transport"].includes(item?.item_type),
  );
}

function renderDailyStays(plan) {
  const stays = Array.isArray(plan.daily_stays) ? plan.daily_stays : [];
  if (!stays.length) return "";
  const recommended = plan.recommended_hotel;
  if (
    recommended &&
    stays.every((stay) => {
      const hotel = stay.end_hotel || stay.start_hotel;
      return (
        hotel?.name === recommended.name &&
        normalizeAddress(hotel.location?.address) ===
          normalizeAddress(recommended.location?.address)
      );
    })
  ) {
    return `<p class="mini-text stay-days-note">住宿安排：${stays.map((stay) => `第 ${Number(stay.day_index)} 天`).join("、")}使用上述酒店。</p>`;
  }
  return `<div class="daily-stay-list">${stays
    .map((stay) => {
      const hotel = stay.end_hotel || stay.start_hotel;
      if (!hotel) return "";
      const address = normalizeAddress(hotel.location?.address);
      const price = stay.charged_night
        ? `${hotelPriceLabel(hotel)} / 间夜`
        : "当日不新增住宿费";
      return `<article class="daily-stay-card"><div class="daily-stay-head"><strong>第 ${Number(stay.day_index || 0)} 天</strong><span>${escapeHtml(price)}</span></div><h3 class="mini-title">${escapeHtml(hotel.name)}</h3>${renderPlaceAddress(hotel.name, address)}${renderPlaceMap(hotel.name, address)}${renderHotelActions(hotel, stay.day_index)}</article>`;
    })
    .join("")}</div>`;
}

function renderMetrics(plan, payload) {
  const metrics = [
    ["旅行天数", `${plan.daily_plans.length}<small>天</small>`],
    ["精选景点", `${plan.selected_attractions.length}<small>处</small>`],
    ["同行人数", `${Number(payload.travelers)}<small>人</small>`],
    [
      "预计花费",
      `¥${Number(plan.budget.total).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`,
    ],
  ];
  return `<div class="metric-grid">${metrics.map(([label, value]) => `<div class="metric"><div class="metric-label">${label}</div><div class="metric-value">${value}</div></div>`).join("")}</div>`;
}

function switchResultPanel(name) {
  document.querySelectorAll(".report-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === name);
    panel.hidden = panel.id !== name;
  });
  els.resultNav.querySelectorAll(".nav-link").forEach((button) => {
    button.classList.toggle("active", button.dataset.target === name);
    button.setAttribute(
      "aria-current",
      button.dataset.target === name ? "page" : "false",
    );
  });
}

function bindResultNav() {
  els.resultNav.hidden = false;
  els.resultNav.querySelectorAll(".nav-link").forEach((button) => {
    button.onclick = () => {
      switchResultPanel(button.dataset.target);
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
  });
  document.querySelectorAll(".day-select").forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll(".day-select").forEach((item) => {
        item.classList.toggle("active", item === button);
        item.setAttribute("aria-pressed", String(item === button));
      });
      document.querySelectorAll(".day-card").forEach((day) => {
        day.classList.toggle("active", day.id === button.dataset.day);
        day.hidden = day.id !== button.dataset.day;
      });
    };
  });
  switchResultPanel("overview");
}

function weatherLabel(value) {
  return (
    {
      sunny: "晴",
      cloudy: "多云",
      overcast: "阴",
      light_rain: "小雨",
      rain: "雨",
      heavy_rain: "大雨",
      snow: "雪",
      hot: "炎热",
      cold: "寒冷",
      unknown: "暂无预报",
    }[value] ||
    value ||
    "天气待确认"
  );
}

function weatherSummary(weather) {
  if (
    !weather ||
    weather.condition === "unknown" ||
    weather.low_c == null ||
    weather.high_c == null
  ) {
    return "暂无天气预报";
  }
  return `${weatherLabel(weather.condition)} · ${weather.low_c}–${weather.high_c}°C`;
}

function dayStopNames(day) {
  return getVisibleDayItems(day)
    .filter(
      (item) => item.item_type !== "transport" && item.item_type !== "meal",
    )
    .map((item) => item.location_name || item.title)
    .join("、");
}

function renderDayPreview(plan) {
  return `<section class="report-section day-preview-section"><div class="section-title-row"><h2>每天怎么玩</h2><span class="section-tag">点开查看当天安排</span></div><div class="day-preview-list">${plan.daily_plans
    .map((day, index) => {
      const stops = getVisibleDayItems(day).filter(
        (p) => p.item_type !== "transport" && p.item_type !== "meal",
      );
      const meals = getVisibleDayItems(day).filter(
        (p) => p.item_type === "meal",
      );
      return `<button type="button" class="day-preview" data-preview-day="${index + 1}"><span class="day-preview-number">DAY<strong>${String(index + 1).padStart(2, "0")}</strong></span><span class="day-preview-copy"><span class="day-preview-date">${escapeHtml(day.date)} · ${escapeHtml(weatherSummary(day.weather))}</span><strong>${escapeHtml(dayStopNames(day) || "自由探索")}</strong><span>${stops.length} 处停留${meals.length ? ` · ${meals.length} 餐安排` : ""} · 查看当天地点</span></span><span class="preview-arrow" aria-hidden="true">↗</span></button>`;
    })
    .join("")}</div></section>`;
}

function renderPlan(plan, request = buildPayload()) {
  renderSources(plan);
  document.querySelector("#sourceDetails").hidden = false;
  document.querySelector("#exportBtn").hidden = false;
  document.querySelector("#resultHeading").textContent =
    `${plan.city} · ${plan.trip_days} 日旅行`;
  document.querySelector("#tripDateLabel").textContent =
    `${request.start_date} — ${request.end_date} · ${request.travelers} 人同行`;
  document.querySelector("#workspaceTitle").textContent = `${plan.city}行程`;
  preparePlaces(plan);
  const addressMap = buildLocationAddressMap(plan);
  const days = plan.daily_plans
    .map(
      (
        day,
        index,
      ) => `<article class="day-card ${index === 0 ? "active" : ""}" id="day-${index + 1}" ${index ? "hidden" : ""}>
    <div class="day-head"><h3>第 ${index + 1} 天 · ${escapeHtml(day.date)}</h3><span class="mini-badge weather-pill"><span class="weather-dot"></span>${escapeHtml(weatherSummary(day.weather))}</span></div>
    <div class="day-summary"><p class="route-summary">${getVisibleDayItems(day).filter((item) => item.item_type !== "meal").length} 处游玩 · ${getVisibleDayItems(day).filter((item) => item.item_type === "meal").length} 餐安排</p></div>
    <div class="item-list">${getVisibleDayItems(day)
      .map((item) => renderPlanItem(item, addressMap, index + 1))
      .join(
        "",
      )}</div>${renderDailyHotelCard(getDailyStayForDay(plan, index), index, getDayHotelItem(day))}</article>`,
    )
    .join("");
  const budgetItems = [
    ["hotel", "住宿", "#6e9584"],
    ["food", "餐饮", "#b99c71"],
    ["attractions", "景点门票", "#96a6bb"],
    ["transport", "交通", "#bc8b76"],
    ["contingency", "弹性预算", "#a4a797"],
  ];
  const total = Number(plan.budget.total) || 0;
  const overBudget = Math.max(0, total - Number(request.budget_max));
  const budgetNotice =
    overBudget > 0
      ? `<p class="budget-notice">当前预估比预算上限高 ¥${Math.round(overBudget).toLocaleString("zh-CN")}，可调整住宿标准或行程安排后重新规划。</p>`
      : "";
  const ratio = (key) =>
    total ? (Math.max(0, Number(plan.budget[key]) || 0) / total) * 100 : 0;
  const paceLabel =
    { relaxed: "悠闲慢游", balanced: "张弛有度", intense: "充实紧凑" }[
      request.pace
    ] || request.pace;
  const preferenceLabels = request.preferences
    .map((key) => preferenceOptions.find(([value]) => key === value)?.[1])
    .filter(Boolean);
  els.resultView.className = "result-view";
  els.resultView.innerHTML = `
    <div class="report-panel active" id="overview">
      <section class="plan-cover cover-with-photo"><div class="cover-copy"><div class="cover-kicker"><svg class="icon"><use href="#i-pin"/></svg>为你定制的 ${escapeHtml(plan.city)} 之旅</div><h2>${escapeHtml(plan.travel_theme)}</h2><div class="plan-tags"><span>${escapeHtml(paceLabel)}</span><span>${escapeHtml(transportModeLabel(request.transport_mode))}</span>${preferenceLabels.map((text) => `<span>${escapeHtml(text)}</span>`).join("")}</div><details class="cover-overview"><summary>查看行程说明</summary><p>${escapeHtml(plan.overview)}</p></details></div>${plan.selected_attractions[0] ? renderPlacePhoto(plan.selected_attractions[0].name, plan.selected_attractions[0].location?.address) : ""}</section>
      ${renderMetrics(plan, request)}
      ${budgetNotice}
      ${renderDayPreview(plan)}
      <section class="report-section"><div class="section-title-row"><h2>值得停留的地方</h2><span class="section-tag">${plan.selected_attractions.length} 处精选</span></div><div class="destination-grid">${plan.selected_attractions.map((item) => `<article class="destination-card">${renderPlacePhoto(item.name, item.location?.address)}<div class="destination-copy"><p class="destination-category">${escapeHtml(item.category || "城市探索")}${item.recommended_hours ? ` · ${Number(item.recommended_hours)} 小时` : ""}</p><h3>${escapeHtml(item.name)}</h3><p class="destination-description">${escapeHtml(item.summary)}</p>${renderPlaceAddress(item.name, item.location?.address)}${renderPlaceMap(item.name, item.location?.address)}${renderAttractionActions(item)}</div></article>`).join("") || '<p class="mini-text">暂无景点建议</p>'}</div></section>
    </div>
    <section class="report-panel" id="days" hidden><div class="day-selector" role="group" aria-label="选择旅行日期">${plan.daily_plans.map((day, i) => `<button type="button" class="day-select ${i === 0 ? "active" : ""}" data-day="day-${i + 1}" aria-pressed="${i === 0}">第 ${i + 1} 天<small>${escapeHtml(day.date.slice(5))}</small></button>`).join("")}</div>${days}</section>
    <section class="report-panel" id="hotel" hidden><div class="section-title-row"><h2>住得舒服，玩得从容</h2><span class="section-tag">价格为预估</span></div><p class="section-subtitle">查看酒店照片和具体地址，点击高德地图确认位置。</p>${renderHotelCard(plan.recommended_hotel, "推荐落脚点")}${renderDailyStays(plan)}</section>
    <section class="report-panel" id="budget" hidden><div class="section-title-row"><h2>每一份预算，都花在期待上</h2><span class="section-tag">费用预估</span></div><div class="budget-summary"><div><span>${request.travelers} 人旅行 · 预计总花费</span><strong>¥${total.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}</strong></div><div class="budget-person">约 ¥${Math.round(total / Math.max(1, request.travelers)).toLocaleString("zh-CN")} / 人<br><span>以实际消费为准</span></div></div><div class="budget-visual" role="img" aria-label="旅行费用占比">${budgetItems.map(([key, label, color]) => `<span style="width:${ratio(key).toFixed(2)}%;background:${color}" title="${label} ${ratio(key).toFixed(0)}%"></span>`).join("")}</div><div class="budget-list">${budgetItems.map(([key, label, color]) => `<div><span><i style="background:${color}"></i>${label}</span><strong>¥${Number(plan.budget[key] || 0).toFixed(0)}<small>${ratio(key).toFixed(0)}%</small></strong></div>`).join("")}</div><p class="route-map-caption">行程中的门票、住宿与交通费用是估算值，不代表实时可预订价格。</p></section>
    <section class="report-panel" id="risks" hidden><div class="columns"><div class="note-card"><h2>出发前，留意这些</h2><ul class="plain-list">${(plan.risk_alerts || []).map((text) => `<li>${escapeHtml(text)}</li>`).join("") || "<li>暂无额外提醒，请核实天气和开放时间。</li>"}</ul></div><div class="note-card"><h2>行李里，记得带上</h2><ul class="plain-list">${(plan.packing_tips || []).map((text) => `<li>${escapeHtml(text)}</li>`).join("") || "<li>证件、充电器和常用药品</li>"}</ul></div></div>${plan.notes?.length ? `<div class="note-card" style="margin-top:17px"><h2>关于这份行程</h2><ul class="plain-list">${plan.notes.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul></div>` : ""}<details class="research-details"><summary>查看规划依据与运行记录</summary><div class="research-content">${renderAgentDiagnostics(plan)}${renderSearchPlan(plan) || '<p class="mini-text">本次没有额外搜索记录。</p>'}</div></details></section>`;
  bindResultNav();
  document.querySelectorAll("[data-preview-day]").forEach((button) => {
    button.onclick = () => {
      switchResultPanel("days");
      document
        .querySelector(`[data-day="day-${button.dataset.previewDay}"]`)
        ?.click();
      els.resultNav.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  });
  renderConversation();
  updatePortfolioUI();
  loadPlaceDetails(plan).then(() => loadQualityReport(plan, request));
}

async function submitPlan(event) {
  event.preventDefault();
  if (isPlanning) return;
  const payload = buildPayload();
  const validationError = validatePayload(payload);
  const formError = document.querySelector("#formError");
  if (validationError) {
    formError.hidden = false;
    formError.textContent = validationError.detail;
    if (document.querySelector("#tripOptions").contains(validationError.focus))
      document.querySelector("#tripOptions").open = true;
    validationError.focus?.focus();
    return;
  }
  formError.hidden = true;
  resetResultView();
  activeTrip = null;
  isPlanning = true;
  setNavigationBusy(true);
  showPage("result");
  setState("规划中");
  document.querySelector("#resultHeading").textContent =
    `${payload.city} · 正在规划`;
  document.querySelector("#tripDateLabel").textContent =
    `${payload.start_date} — ${payload.end_date} · ${payload.travelers} 人同行`;
  window.scrollTo({ top: 0, behavior: "smooth" });
  startProgress();
  els.submitBtn.disabled = true;
  try {
    const task = await createPlanTask(payload);
    const data = await waitForPlanStream(task.task_id);
    if (!data) throw { detail: "没有收到完整行程，请稍后重试。" };
    activeTrip = {
      id: task.output_id,
      lastTaskId: task.task_id,
      request: payload,
      plan: data,
      createdAt: Date.now(),
      conversation: [],
      revisions: [],
      dayRules: {},
      revisionCount: 0,
    };
    renderPlan(data, payload);
    saveBackgroundResult(task.task_id);
    setState("已完成");
  } catch (error) {
    failProgress();
    renderErrorCard(error);
    setState("未完成");
  } finally {
    isPlanning = false;
    setNavigationBusy(false);
    updateDateSpanValidity();
  }
}

function readSavedTrips() {
  try {
    const records = JSON.parse(localStorage.getItem("roam.trips") || "[]");
    return Array.isArray(records)
      ? records
          .filter(
            (record) =>
              typeof record?.id === "string" &&
              typeof record.plan?.city === "string" &&
              Array.isArray(record.plan.daily_plans) &&
              Array.isArray(record.plan.selected_attractions) &&
              record.request &&
              record.plan.budget,
          )
          .slice(0, 12)
      : [];
  } catch {
    return [];
  }
}

function saveTrip(trip) {
  const next = [
    trip,
    ...readSavedTrips().filter((item) => item.id !== trip.id),
  ].slice(0, 12);
  try {
    localStorage.setItem("roam.trips", JSON.stringify(next));
    savedTrips = next;
    renderHistory();
    showToast("行程已保存，随时回来继续看");
    return true;
  } catch {
    showToast("浏览器存储空间不足，可先导出这份行程");
    return false;
  }
}

function renderHistory() {
  const history = document.querySelector("#tripHistory");
  history.replaceChildren();
  document.querySelector("#historyCount").textContent = savedTrips.length;
  document.querySelector("#historyEmpty").hidden = savedTrips.length > 0;
  savedTrips.forEach((trip) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `history-item ${activeTrip?.id === trip.id ? "active" : ""}`;
    button.setAttribute(
      "aria-label",
      `${trip.plan.city} ${trip.plan.trip_days} 日旅行，${trip.request.start_date}`,
    );
    button.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-map"/></svg><span><strong>${escapeHtml(trip.plan.city)} · ${Number(trip.plan.trip_days)} 日旅行</strong><small>${escapeHtml(trip.request.start_date)} · ${Number(trip.request.travelers)} 人</small></span>`;
    button.disabled = isPlanning;
    button.onclick = () => {
      if (isPlanning) return;
      activeTrip = trip;
      applyRequest(trip.request);
      showPage("result");
      renderPlan(trip.plan, trip.request);
      setState("已保存");
      els.progressPanel.hidden = true;
      renderHistory();
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
    history.appendChild(button);
  });
}

function applyRequest(request) {
  const fields = {
    city: "city",
    startDate: "start_date",
    endDate: "end_date",
    budgetMin: "budget_min",
    budgetMax: "budget_max",
    extraPreferences: "extra_preferences",
    taboos: "taboos",
    travelers: "travelers",
    pace: "pace",
    hotelStyle: "hotel_style",
    transportMode: "transport_mode",
    transitPreference: "transit_preference",
  };
  Object.entries(fields).forEach(([element, key]) => {
    if (request[key] !== undefined) els[element].value = request[key];
  });
  selectedPreferences.clear();
  (request.preferences || []).forEach((item) => selectedPreferences.add(item));
  renderPreferenceChips();
  updateTransitPreferenceVisibility();
  updateDateSpanValidity();
  updateOptionsSummary();
}

function localDate(offset) {
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() + offset);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function newTrip() {
  if (isPlanning) return;
  activeTrip = null;
  els.form.reset();
  applyRequest({
    city: "",
    start_date: localDate(1),
    end_date: localDate(3),
    preferences: ["humanity", "food"],
  });
  document.querySelector("#tripOptions").open = false;
  document.querySelector("#formError").hidden = true;
  showPage("form");
  renderHistory();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function updateOptionsSummary() {
  document.querySelector("#optionsSummary").textContent =
    `${els.travelers.value || 1} 人 · ¥${Number(els.budgetMin.value).toLocaleString("zh-CN")}–${Number(els.budgetMax.value).toLocaleString("zh-CN")} · ${transportModeLabel(els.transportMode.value)}`;
}

function showToast(text) {
  const toast = document.querySelector("#toast");
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.hidden = false;
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 3500);
}

function closeSidebar() {
  document.body.classList.remove("sidebar-open");
  document.querySelector(".workspace").inert = false;
  document.querySelector("#sidebarBackdrop").hidden = true;
  document.querySelector("#menuBtn").setAttribute("aria-expanded", "false");
  if (matchMedia("(max-width:760px)").matches)
    document.querySelector("#sidebar").inert = true;
}

function setNavigationBusy(busy) {
  ["newTripBtn", "brandHome", "plannerHome", "backToFormBtn"].forEach((id) => {
    document.getElementById(id).disabled = busy;
  });
  document.querySelectorAll(".history-item").forEach((button) => {
    button.disabled = busy;
  });
  syncStopButtons(busy);
  els.form.setAttribute("aria-busy", String(busy));
}

function progressPresentation(stage) {
  const percent = currentProgressPercent;
  if (percent === 100)
    return {
      title: "旅程已就位，准备出发",
      detail: "每日安排、住宿建议与预算已为你整理好。",
      step: 4,
    };
  if (stage.title === "生成过程遇到问题")
    return {
      title: "这次规划遇到一点问题",
      detail: "请查看下方提示，调整需求后可以重新尝试。",
      step: 0,
    };
  if (percent < 15)
    return {
      title: "先了解你的旅行期待",
      detail: "正在整理目的地、时间、预算和旅行偏好。",
      step: 0,
    };
  if (percent < 58)
    return {
      title: "探索值得停留的地方",
      detail: "正在查找景点，并结合天气与住宿安排进行筛选。",
      step: 1,
    };
  if (percent < 84)
    return {
      title: "把期待排进每一天",
      detail: "正在编排景点、餐饮与住宿，让旅途张弛有度。",
      step: 2,
    };
  return {
    title: "完善最后一段路线",
    detail: "正在整理交通建议、费用预估与出发前的提醒。",
    step: 3,
  };
}

function exportTrip() {
  if (!activeTrip) return;
  const { plan, request } = activeTrip;
  const lines = [
    `# ${plan.city} · ${plan.travel_theme}`,
    "",
    `${request.start_date} 至 ${request.end_date} · ${request.travelers} 人`,
    "",
    plan.overview,
    "",
    "## 每日行程",
  ];
  plan.daily_plans.forEach((day, i) => {
    lines.push("", `### 第 ${i + 1} 天 · ${day.date}`, "");
    (day.items || [])
      .filter((item) => item.item_type !== "transport")
      .forEach((item) => {
        lines.push(
          `- ${item.time_range} ${item.title}（约 ¥${Number(item.estimated_cost || 0).toFixed(0)}）`,
          `  ${item.summary || ""}`,
        );
        if (item.item_type !== "transport") {
          const name = item.location_name || item.title;
          const ref = findPlace(name, item.location_address);
          const detail = placeDetails[ref?.key];
          const address = detail?.address || item.location_address;
          if (address) lines.push(`  地址：${address}`);
          if (isOpeningStop(item)) {
            const check = openingVisitCheck(item, day.date);
            lines.push(
              `  开放核对：${check?.hours_text || "开放时间待确认"}${check ? `；${check.message}` : ""}`,
            );
            if (check?.raw_text) lines.push(`  原始规则：${check.raw_text}`);
            if (check?.source)
              lines.push(
                `  来源：${check.source}；查询时间：${check.checked_at}`,
              );
          }
          lines.push(
            `  [高德地图](${safePlaceUrl(detail?.map_url, true) || fallbackMapUrl(name)})`,
          );
        }
      });
  });
  lines.push(
    "",
    "## 预算预估",
    `总计 ¥${plan.budget.total}`,
    `住宿 ¥${plan.budget.hotel} · 餐饮 ¥${plan.budget.food} · 景点 ¥${plan.budget.attractions} · 交通 ¥${plan.budget.transport} · 弹性 ¥${plan.budget.contingency}`,
    "",
    "## 出行提醒",
    ...(plan.risk_alerts || []).map((text) => `- ${text}`),
    "",
    "## 携带建议",
    ...(plan.packing_tips || []).map((text) => `- ${text}`),
    "",
    "## 数据来源",
    `规划：${sourceLabel(plan.planning_source)}；景点：${sourceLabel(plan.attraction_data_source)}；天气：${sourceLabel(plan.weather_data_source)}；酒店：${sourceLabel(plan.hotel_data_source)}`,
    "费用为预估，出发前请核实天气、开放时间与实际价格。",
  );
  const url = URL.createObjectURL(
    new Blob(["\ufeff" + lines.join("\n")], {
      type: "text/markdown;charset=utf-8",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `${plan.city.replace(/[\\/:*?"<>|]/g, "")}-${request.start_date}-旅行计划.md`;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  showToast("已发起下载，请查看浏览器下载列表");
}

function renderChangeDetails(changes = []) {
  return changes
    .map(
      (change) =>
        `<div class="revision-change"><strong>${escapeHtml(change.section)}</strong><div class="change-before"><span>调整前</span><p>${escapeHtml(change.before)}</p></div><div class="change-after"><span>调整后</span><p>${escapeHtml(change.after)}</p></div></div>`,
    )
    .join("");
}

let conversationTripId = null;

function renderConversation() {
  if (!activeTrip) return;
  document.querySelector("#openRevisionBtn").hidden = false;
  if (conversationTripId !== activeTrip.id) {
    conversationTripId = activeTrip.id;
    closeRevisionDrawer(false);
    document.querySelector("#revisionInput").value = "";
    document.querySelector("#revisionError").hidden = true;
  }
  const messages = activeTrip.conversation || [];
  const log = document.querySelector("#revisionMessages");
  log.innerHTML =
    `<div class="conversation-welcome"><span class="conversation-avatar" aria-hidden="true">漫</span><p>行程已经有了雏形。想少走一点路、换一天的安排，或看看预算？把想法告诉我，我们接着调整。</p></div>` +
    messages
      .map(
        (message) =>
          `<article class="conversation-message ${message.role === "user" ? "from-user" : "from-assistant"}"><span class="message-author">${message.role === "user" ? "你" : "漫游"}</span><p>${escapeHtml(message.content)}</p>${message.warnings?.length ? `<ul class="message-warnings">${message.warnings.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>` : ""}${message.changes?.length ? `<details class="message-changes"><summary>查看本轮 ${message.changes.length} 项变化</summary>${renderChangeDetails(message.changes)}</details>` : ""}</article>`,
      )
      .join("");
  log.scrollTop = log.scrollHeight;
  const count = activeTrip.revisionCount || 0;
  document.querySelector("#revisionVersion").textContent = count
    ? `第 ${count} 次调整`
    : "初始版本";
  document.querySelector("#undoRevisionBtn").disabled =
    isPlanning || !activeTrip.revisions?.length;
  const summary = document.querySelector("#revisionSummary");
  summary.hidden = !activeTrip.lastRevision;
  if (activeTrip.lastRevision) {
    summary.innerHTML = `<div class="revision-summary-heading"><span class="revision-check" aria-hidden="true">✓</span><div><strong>行程已更新</strong><p>${escapeHtml(activeTrip.lastRevision.message)}</p></div></div>${activeTrip.lastRevision.warnings?.length ? `<ul class="message-warnings">${activeTrip.lastRevision.warnings.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>` : ""}<details class="revision-summary-details"><summary>对比调整前后</summary>${renderChangeDetails(activeTrip.lastRevision.changes)}</details>`;
  }
}

function revisionSnapshot(trip) {
  return structuredClone({
    request: trip.request,
    plan: trip.plan,
    dayRules: trip.dayRules || {},
    conversation: trip.conversation || [],
    lastRevision: trip.lastRevision || null,
    lastTaskId: trip.lastTaskId || null,
    revisionCount: trip.revisionCount || 0,
  });
}

function setRevisionBusy(busy) {
  busy = busy || activeTrip?.mode === "recorded_demo";
  document.querySelector("#revisionSendBtn").disabled = busy;
  document.querySelector("#revisionInput").disabled = busy;
  document
    .querySelector("#revisionForm")
    .setAttribute("aria-busy", String(busy));
  document.querySelector("#revisionProgress").hidden = !busy;
  document.querySelectorAll("[data-revision-suggestion]").forEach((button) => {
    button.disabled = busy;
  });
  document.querySelector("#undoRevisionBtn").disabled =
    busy || !activeTrip?.revisions?.length;
}

function commitTripRevision(result, previous) {
  if (result.status === "revised") {
    activeTrip.revisions = [...(activeTrip.revisions || []), previous].slice(
      -5,
    );
    activeTrip.request = result.request;
    activeTrip.plan = result.plan;
    activeTrip.dayRules = result.day_rules;
    activeTrip.revisionCount = previous.revisionCount + 1;
    activeTrip.lastRevision = {
      message: result.message,
      changes: result.changes,
      warnings: result.warnings,
    };
    activeTrip.updatedAt = Date.now();
  }
  activeTrip.conversation.push({
    role: "assistant",
    content: result.message,
    changes: result.changes,
    warnings: result.warnings,
  });
  activeTrip.conversation = activeTrip.conversation.slice(-24);
}

async function submitRevision(event) {
  event.preventDefault();
  if (isPlanning || !activeTrip || activeTrip.mode === "recorded_demo") return;
  const input = document.querySelector("#revisionInput");
  const message = input.value.trim();
  if (!message) return;
  const previous = revisionSnapshot(activeTrip);
  const previousRevisions = (activeTrip.revisions || []).slice();
  const errorBox = document.querySelector("#revisionError");
  errorBox.hidden = true;
  isPlanning = true;
  setNavigationBusy(true);
  setRevisionBusy(true);
  setState("调整中");
  document.querySelector("#revisionProgress").textContent =
    "正在连接行程调整服务…";
  activeTrip = {
    ...activeTrip,
    conversation: [
      ...previous.conversation,
      { role: "user", content: message },
    ],
  };
  renderConversation();
  try {
    const task = await postBackgroundTask(
      "revision",
      {
        request: previous.request,
        plan: previous.plan,
        message,
        history: previous.conversation.slice(-16).map(({ role, content }) => ({
          role,
          content: content.slice(0, 1200),
        })),
        day_rules: previous.dayRules,
      },
      { message },
    );
    const result = await waitForPlanStream(task.task_id, {
      revision: true,
      onProgress: (data) => {
        if (data.stage !== "connected" && data.stage !== "queued")
          document.querySelector("#revisionProgress").textContent =
            `${data.title} · ${data.detail}`;
      },
    });
    if (
      !result?.plan ||
      !["revised", "clarification", "answered"].includes(result.status)
    )
      throw { detail: "未收到完整调整结果，请重试。" };
    commitTripRevision(result, previous);
    activeTrip.lastTaskId = task.task_id;
    input.value = "";
    applyRequest(activeTrip.request);
    renderPlan(activeTrip.plan, activeTrip.request);
    saveBackgroundResult(task.task_id);
    setState(
      result.status === "revised"
        ? "已调整"
        : result.status === "clarification"
          ? "待补充"
          : "已回复",
    );
    if (result.status === "revised") {
      const firstDay = result.changes
        .find((item) => /^第 \d+ 天/.test(item.section))
        ?.section.match(/^第 (\d+) 天/)[1];
      if (firstDay) {
        switchResultPanel("days");
        document
          .querySelector(`.day-select[data-day="day-${firstDay}"]`)
          ?.click();
      }
    }
  } catch (error) {
    stopProgress();
    activeTrip = { ...activeTrip, ...previous, revisions: previousRevisions };
    applyRequest(activeTrip.request);
    renderPlan(activeTrip.plan, activeTrip.request);
    errorBox.textContent =
      extractErrorDetail(error) || "暂时无法完成这次调整，请稍后重试。";
    errorBox.hidden = false;
    setState("调整未完成");
  } finally {
    isPlanning = false;
    setNavigationBusy(false);
    setRevisionBusy(false);
    updateDateSpanValidity();
    input.focus({ preventScroll: true });
  }
}

function undoRevision() {
  if (activeTrip?.mode === "recorded_demo") return;
  if (isPlanning || !activeTrip?.revisions?.length) return;
  const revisions = activeTrip.revisions.slice();
  const previous = revisions.pop();
  activeTrip = {
    ...activeTrip,
    ...structuredClone(previous),
    revisions,
    updatedAt: Date.now(),
  };
  activeTrip.conversation.push({
    role: "assistant",
    content: "已撤销上次调整，恢复了对应的行程、旅行需求和预算。",
  });
  activeTrip.conversation = activeTrip.conversation.slice(-24);
  document.querySelector("#revisionError").hidden = true;
  applyRequest(activeTrip.request);
  renderPlan(activeTrip.plan, activeTrip.request);
  saveTrip(activeTrip);
  setState("已恢复");
}

let drawerInertElements = [];
let drawerReturnFocus = null;

function openRevisionDrawer() {
  const panel = document.querySelector("#revisionPanel");
  if (!panel.hidden || !activeTrip || activeTrip.mode === "recorded_demo")
    return;
  drawerReturnFocus = document.activeElement;
  panel.hidden = false;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  document.querySelector("#revisionBackdrop").hidden = false;
  document
    .querySelector("#openRevisionBtn")
    .setAttribute("aria-expanded", "true");
  document.body.classList.add("revision-open");
  drawerInertElements = [];
  let branch = panel;
  while (branch.parentElement) {
    for (const sibling of branch.parentElement.children) {
      if (
        sibling === branch ||
        sibling.id === "revisionBackdrop" ||
        ["SCRIPT", "LINK", "STYLE"].includes(sibling.tagName)
      )
        continue;
      drawerInertElements.push([sibling, sibling.inert]);
      sibling.inert = true;
    }
    branch = branch.parentElement;
    if (branch === document.body) break;
  }
  document.querySelector("#revisionInput").focus({ preventScroll: true });
}

function closeRevisionDrawer(restoreFocus = true) {
  const panel = document.querySelector("#revisionPanel");
  const wasOpen = !panel.hidden;
  panel.hidden = true;
  panel.removeAttribute("role");
  panel.removeAttribute("aria-modal");
  document.querySelector("#revisionBackdrop").hidden = true;
  document
    .querySelector("#openRevisionBtn")
    .setAttribute("aria-expanded", "false");
  document.body.classList.remove("revision-open");
  for (const [el, previous] of drawerInertElements) el.inert = previous;
  drawerInertElements = [];
  if (wasOpen && restoreFocus)
    drawerReturnFocus?.focus({ preventScroll: true });
}

function initializeConversation() {
  document
    .querySelector("#revisionForm")
    .addEventListener("submit", submitRevision);
  document.querySelector("#undoRevisionBtn").onclick = undoRevision;
  document.querySelector("#openRevisionBtn").onclick = openRevisionDrawer;
  document.querySelector("#closeRevisionBtn").onclick = () =>
    closeRevisionDrawer();
  document.querySelector("#revisionBackdrop").onclick = () =>
    closeRevisionDrawer();
  document.addEventListener("keydown", (event) => {
    const panel = document.querySelector("#revisionPanel");
    if (panel.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeRevisionDrawer();
    }
    if (event.key === "Tab") {
      const focusable = [
        ...panel.querySelectorAll(
          "button:not(:disabled), textarea:not(:disabled), input:not(:disabled), a[href], summary",
        ),
      ].filter((el) => el.getClientRects().length);
      const first = focusable[0],
        last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
  });
  document.querySelectorAll("[data-revision-suggestion]").forEach((button) => {
    button.onclick = () => {
      document.querySelector("#revisionInput").value =
        button.dataset.revisionSuggestion;
      document.querySelector("#revisionInput").focus();
    };
  });
  document
    .querySelector("#revisionInput")
    .addEventListener("keydown", (event) => {
      if (
        event.key === "Enter" &&
        (event.ctrlKey || event.metaKey) &&
        !event.isComposing
      ) {
        event.preventDefault();
        document.querySelector("#revisionForm").requestSubmit();
      }
    });
}

function initializeWorkspace() {
  els.startDate.value = localDate(1);
  els.endDate.value = localDate(3);
  updateOptionsSummary();
  renderHistory();
  const mobile = matchMedia("(max-width:760px)");
  document.querySelector("#sidebar").inert = mobile.matches;
  mobile.addEventListener("change", () => {
    closeSidebar();
    document.querySelector("#sidebar").inert = mobile.matches;
  });
  document.querySelector("#menuBtn").onclick = () => {
    document.body.classList.add("sidebar-open");
    document.querySelector(".workspace").inert = true;
    document.querySelector("#sidebar").inert = false;
    document.querySelector("#sidebarBackdrop").hidden = false;
    document.querySelector("#menuBtn").setAttribute("aria-expanded", "true");
    document.querySelector("#newTripBtn").focus();
  };
  document.querySelector("#sidebarBackdrop").onclick = closeSidebar;
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Escape" &&
      document.body.classList.contains("sidebar-open")
    ) {
      closeSidebar();
      document.querySelector("#menuBtn").focus();
    }
  });
  ["newTripBtn", "brandHome", "plannerHome"].forEach((id) => {
    document.getElementById(id).onclick = newTrip;
  });
  const syncTheme = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    document
      .querySelector("#themeIcon")
      .setAttribute("href", dark ? "#i-sun" : "#i-moon");
    document
      .querySelector("#themeBtn")
      .setAttribute("aria-label", dark ? "切换到浅色模式" : "切换到深色模式");
    document.querySelector('meta[name="theme-color"]').content = dark
      ? "#1d211f"
      : "#faf9f6";
  };
  syncTheme();
  document.querySelector("#themeBtn").onclick = () => {
    const theme =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("roam.theme", theme);
    } catch {}
    syncTheme();
  };
  document.querySelector("#exportBtn").onclick = exportTrip;
  els.form.addEventListener("input", updateOptionsSummary);
  els.form.addEventListener("change", updateOptionsSummary);
  els.form.addEventListener(
    "invalid",
    (event) => {
      if (document.querySelector("#tripOptions").contains(event.target))
        document.querySelector("#tripOptions").open = true;
    },
    true,
  );
  const presets = {
    hangzhou: {
      city: "杭州",
      days: 3,
      preferences: ["humanity", "nature", "food"],
      extra_preferences: "想沿西湖散步，看看江南老街，节奏轻松一点。",
      pace: "relaxed",
      budget_min: 1200,
      budget_max: 2500,
    },
    chengdu: {
      city: "成都",
      days: 3,
      preferences: ["food", "humanity"],
      extra_preferences: "想体验地道川味，逛逛老街和茶馆。",
      pace: "relaxed",
      budget_min: 1200,
      budget_max: 2500,
    },
    beijing: {
      city: "北京",
      days: 2,
      preferences: ["humanity", "art"],
      extra_preferences: "想看古都建筑，逛胡同，感受历史文化。",
      pace: "balanced",
      budget_min: 1000,
      budget_max: 2000,
    },
  };
  document.querySelectorAll("[data-preset]").forEach((button) => {
    button.onclick = () => {
      const preset = presets[button.dataset.preset];
      applyRequest({
        ...preset,
        start_date: localDate(1),
        end_date: localDate(preset.days),
      });
      showToast(`已填好${preset.city}旅行灵感，可以按喜好调整`);
      els.city.focus({ preventScroll: true });
    };
  });
}

function bindGlobalEvents() {
  els.transportMode.addEventListener(
    "change",
    updateTransitPreferenceVisibility,
  );
  [els.startDate, els.endDate].forEach((input) => {
    input.addEventListener("input", () =>
      updateDateSpanValidity({ report: true }),
    );
    input.addEventListener("change", () =>
      updateDateSpanValidity({ report: true }),
    );
  });
  els.form.addEventListener("submit", submitPlan);
  els.backToFormBtn.addEventListener("click", () => {
    stopProgress();
    els.progressPanel.hidden = true;
    showPage("form");
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

initializeWorkspace();
initializeConversation();
renderPreferenceChips();
updateTransitPreferenceVisibility();
bindGlobalEvents();
loadHealth().catch(() => {
  els.healthText.textContent = "暂未连接服务";
  els.healthText.classList.add("offline");
});

initializePortfolio();
initializeTaskRecovery();
