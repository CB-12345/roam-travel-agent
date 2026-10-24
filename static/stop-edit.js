// 地点编辑只提交服务器签发的候选令牌；具体价格、坐标由服务器保留。
const stopEditDialog = document.querySelector("#stopEditDialog");
let stopEditSession = null;

function sameStop(a, b) {
  return (
    a.kind === b.kind &&
    a.name === b.name &&
    a.address === b.address &&
    (a.kind === "hotel" ||
      (a.day_index === b.day_index && a.meal_type === b.meal_type))
  );
}

function stopIsLocked(target) {
  return (activeTrip?.plan.place_locks || []).some((lock) =>
    sameStop(lock, target),
  );
}

function renderStopActions(
  kind,
  dayIndex,
  name,
  address = "",
  mealType = "",
  concrete = true,
) {
  if (activeTrip?.mode === "recorded_demo") return "";
  if (!dayIndex || !name || !["attraction", "meal", "hotel"].includes(kind))
    return "";
  const target = {
    kind,
    day_index: dayIndex,
    name,
    address,
    meal_type: mealType,
  };
  const locked = stopIsLocked(target);
  const data = escapeHtml(JSON.stringify(target));
  return `<div class="stop-edit-actions" role="group" aria-label="调整${escapeHtml(name)}">
    <button type="button" class="stop-edit-button" data-stop-replace data-stop-target="${data}" ${locked || isPlanning ? "disabled" : ""} aria-label="更换${escapeHtml(name)}">↻ <span>换一个</span></button>
    ${concrete ? `<button type="button" class="stop-edit-button stop-lock-button" data-stop-lock data-stop-target="${data}" aria-pressed="${locked}" aria-label="${locked ? "解锁" : "锁定"}${escapeHtml(name)}" title="锁定后保留地点和所在日期；时间可随交通调整。同一家酒店的锁定适用于全部住宿日期。" ${isPlanning ? "disabled" : ""}>${locked ? "✓ 已锁定" : "锁定安排"}</button>` : '<span class="stop-lock-hint">确定具体地点后可锁定</span>'}
  </div>`;
}

function renderAttractionActions(point) {
  const dayIndex = activeTrip?.plan.daily_plans.findIndex((day) =>
    day.items.some(
      (item) =>
        item.item_type === "attraction" && item.location_name === point.name,
    ),
  );
  if (dayIndex == null || dayIndex < 0) return "";
  const item = activeTrip.plan.daily_plans[dayIndex].items.find(
    (item) =>
      item.item_type === "attraction" && item.location_name === point.name,
  );
  return renderStopActions(
    "attraction",
    dayIndex + 1,
    item.location_name,
    item.location_address || "",
  );
}

function renderHotelActions(hotel, dayIndex = null) {
  if (!hotel) return "";
  const index =
    dayIndex ||
    activeTrip?.plan.daily_stays.find((stay) =>
      [stay.start_hotel, stay.end_hotel].some(
        (h) =>
          h?.name === hotel.name &&
          h.location.address === hotel.location.address,
      ),
    )?.day_index;
  return renderStopActions(
    "hotel",
    index,
    hotel.name,
    hotel.location?.address || "",
  );
}

function syncStopButtons(busy = isPlanning) {
  document.querySelectorAll("[data-stop-target]").forEach((button) => {
    const target = JSON.parse(button.dataset.stopTarget);
    const locked = stopIsLocked(target);
    button.disabled =
      busy || (button.hasAttribute("data-stop-replace") && locked);
    if (button.hasAttribute("data-stop-lock")) {
      button.setAttribute("aria-pressed", String(locked));
      button.setAttribute(
        "aria-label",
        `${locked ? "解锁" : "锁定"}${target.name}`,
      );
      button.textContent = locked ? "✓ 已锁定" : "锁定安排";
    }
  });
}

function toggleStopLock(target) {
  if (isPlanning || !activeTrip || activeTrip.mode === "recorded_demo") return;
  const previous = revisionSnapshot(activeTrip);
  const locked = stopIsLocked(target);
  const locks = activeTrip.plan.place_locks || [];
  activeTrip.plan.place_locks = locked
    ? locks.filter((lock) => !sameStop(lock, target))
    : [...locks, target];
  activeTrip.revisions = [...(activeTrip.revisions || []), previous].slice(-5);
  activeTrip.revisionCount = previous.revisionCount + 1;
  const message = locked
    ? `已解锁「${target.name}」。`
    : `已锁定「${target.name}」${target.kind === "hotel" ? "的全部住宿日期" : `在第 ${target.day_index} 天的安排`}。后续调整会保留这个地点，时间可随交通变化。`;
  activeTrip.lastRevision = {
    message,
    changes: [
      {
        section: "地点锁定",
        before: locked ? "已锁定" : "未锁定",
        after: locked ? "未锁定" : "已锁定",
      },
    ],
    warnings: [],
  };
  activeTrip.conversation = [
    ...(activeTrip.conversation || []),
    { role: "assistant", content: message },
  ].slice(-24);
  activeTrip.updatedAt = Date.now();
  syncStopButtons();
  renderConversation();
  saveTrip(activeTrip);
  showToast(locked ? "已解锁，可以继续更换" : "已锁定，后续调整将保留此地点");
}

function closeStopEditor() {
  if (stopEditSession?.applying) return;
  stopEditSession?.controller.abort();
  stopEditSession = null;
  stopEditDialog.close();
  document.body.classList.remove("stop-editor-open");
}

async function openStopEditor(target) {
  if (isPlanning || !activeTrip || stopIsLocked(target)) return;
  closeRevisionDrawer(false);
  const session = {
    tripId: activeTrip.id,
    controller: new AbortController(),
    applying: false,
    context: structuredClone({
      request: activeTrip.request,
      plan: activeTrip.plan,
      target,
      day_rules: activeTrip.dayRules || {},
    }),
    panel: document.querySelector(".report-panel.active")?.id || "days",
    day:
      document.querySelector(".day-select.active")?.dataset.day ||
      `day-${target.day_index}`,
  };
  stopEditSession = session;
  document.querySelector("#stopEditHeading").textContent = {
    attraction: "换一个景点",
    meal: "换一家餐厅",
    hotel: "换一家酒店",
  }[target.kind];
  document.querySelector("#stopEditOriginal").textContent = target.name;
  document.querySelector("#stopEditScope").textContent =
    "正在查找附近符合条件的真实地点…";
  document.querySelector("#stopEditStatus").textContent =
    "正在查询地图，当前安排已保留。";
  document.querySelector("#stopEditStatus").setAttribute("role", "status");
  document.querySelector("#stopEditCandidates").replaceChildren();
  document.querySelector("#stopEditClose").disabled = false;
  stopEditDialog.setAttribute("aria-busy", "true");
  stopEditDialog.showModal();
  document.body.classList.add("stop-editor-open");
  try {
    const response = await fetch("/api/plan/places/candidates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(session.context),
      signal: session.controller.signal,
    });
    const data = await response.json();
    if (!response.ok) throw data;
    if (session !== stopEditSession || session.tripId !== activeTrip?.id)
      return;
    document.querySelector("#stopEditScope").textContent =
      target.kind === "hotel"
        ? `同步更新第 ${data.affected_days.join("、")} 天使用的这家酒店，保留连续住宿。`
        : `只更换第 ${target.day_index} 天的这处地点，并校准当天时间与费用。`;
    document.querySelector("#stopEditStatus").textContent = data.candidates
      .length
      ? "选定后会校验相邻交通和完整停留时间；不合适时保留原安排。"
      : data.message;
    session.candidates = data.candidates;
    document.querySelector("#stopEditCandidates").innerHTML = data.candidates
      .map((candidate, index) => {
        const delta = Number(candidate.cost_delta);
        const price =
          target.kind === "hotel"
            ? `${hotelPriceLabel({ ...candidate, nightly_price: candidate.estimated_cost })} / 间夜`
            : `预计 ¥${Math.round(candidate.estimated_cost)}${target.kind === "meal" ? ` / ${session.context.request.travelers} 人` : ""}`;
        const difference =
          delta === 0
            ? "费用预估持平"
            : `${target.kind === "hotel" ? "住宿合计" : "本次停留"}约${delta > 0 ? "增加" : "节省"} ¥${Math.round(Math.abs(delta))}`;
        return `<article class="stop-candidate"><div class="candidate-topline"><span class="candidate-number">备选 ${String(index + 1).padStart(2, "0")}</span><span>${escapeHtml(difference)}</span></div><h3>${escapeHtml(candidate.name)}</h3><p class="candidate-address">${escapeHtml(candidate.location.address)}</p><div class="candidate-meta"><strong>${escapeHtml(price)}</strong>${candidate.recommended_hours ? `<span>建议 ${Number(candidate.recommended_hours)} 小时</span>` : ""}</div><p class="candidate-reason">${escapeHtml(candidate.reasons[0] || "")}</p><div class="candidate-actions"><a class="place-map-link" href="${escapeHtml(safePlaceUrl(candidate.map_url, true))}" target="_blank" rel="noopener noreferrer">在高德查看 ↗</a><button type="button" class="candidate-choose" data-stop-choice="${index}" aria-label="改为${escapeHtml(candidate.name)}">选这一个 <span aria-hidden="true">↗</span></button></div></article>`;
      })
      .join("");
  } catch (error) {
    if (session !== stopEditSession || error.name === "AbortError") return;
    document.querySelector("#stopEditStatus").setAttribute("role", "alert");
    document.querySelector("#stopEditStatus").textContent =
      extractErrorDetail(error) ||
      "暂时无法查询候选，请稍后重试。原行程已保留。";
  } finally {
    if (session === stopEditSession)
      stopEditDialog.setAttribute("aria-busy", "false");
  }
}

async function applyStopCandidate(index) {
  const session = stopEditSession;
  const candidate = session?.candidates?.[index];
  if (
    !candidate ||
    session.applying ||
    isPlanning ||
    activeTrip?.id !== session.tripId
  )
    return;
  const previous = revisionSnapshot(activeTrip);
  session.applying = true;
  isPlanning = true;
  setNavigationBusy(true);
  setRevisionBusy(true);
  setState("调整中");
  stopEditDialog.setAttribute("aria-busy", "true");
  document.querySelector("#stopEditClose").disabled = true;
  stopEditDialog.querySelectorAll("[data-stop-choice]").forEach((button) => {
    button.disabled = true;
  });
  document.querySelector("#stopEditStatus").setAttribute("role", "status");
  document.querySelector("#stopEditStatus").textContent =
    "正在替换地点并校验时间，请稍候…";
  try {
    const task = await postBackgroundTask(
      "replacement",
      {
        ...session.context,
        candidate_token: candidate.token,
      },
      {
        message: `把「${session.context.target.name}」换成「${candidate.name}」。`,
      },
    );
    const result = await waitForPlanStream(task.task_id, {
      revision: true,
      onProgress: (data) => {
        document.querySelector("#stopEditStatus").textContent =
          `${data.title} · ${data.detail}`;
      },
    });
    if (!result?.plan || result.status !== "revised")
      throw { detail: "未收到完整修改结果，当前行程已保留。" };
    activeTrip.conversation = [
      ...(activeTrip.conversation || []),
      {
        role: "user",
        content: `把「${session.context.target.name}」换成「${candidate.name}」。`,
      },
    ];
    commitTripRevision(result, previous);
    activeTrip.lastTaskId = task.task_id;
    applyRequest(activeTrip.request);
    renderPlan(activeTrip.plan, activeTrip.request);
    switchResultPanel(session.panel);
    document.querySelector(`.day-select[data-day="${session.day}"]`)?.click();
    saveBackgroundResult(task.task_id);
    setState("已调整");
    session.applying = false;
    closeStopEditor();
    showToast("已更换地点，时间与费用已同步更新；可在“调整行程”中撤销");
  } catch (error) {
    stopProgress();
    document.querySelector("#stopEditStatus").setAttribute("role", "alert");
    document.querySelector("#stopEditStatus").textContent =
      extractErrorDetail(error) || "这次调整未完成，原行程已保留。";
    setState("调整未完成");
  } finally {
    session.applying = false;
    isPlanning = false;
    setNavigationBusy(false);
    setRevisionBusy(false);
    stopEditDialog.setAttribute("aria-busy", "false");
    document.querySelector("#stopEditClose").disabled = false;
    stopEditDialog.querySelectorAll("[data-stop-choice]").forEach((button) => {
      button.disabled = false;
    });
    updateDateSpanValidity();
    if (!stopEditDialog.open) {
      [...document.querySelectorAll("[data-stop-replace]")]
        .find(
          (button) =>
            button.getClientRects().length &&
            JSON.parse(button.dataset.stopTarget).name === candidate.name,
        )
        ?.focus({ preventScroll: true });
    }
  }
}

document.querySelector("#resultView").addEventListener("click", (event) => {
  const button = event.target.closest("[data-stop-target]");
  if (!button || button.disabled) return;
  const target = JSON.parse(button.dataset.stopTarget);
  if (button.hasAttribute("data-stop-lock")) toggleStopLock(target);
  else openStopEditor(target);
});
document.querySelector("#stopEditClose").onclick = closeStopEditor;
stopEditDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeStopEditor();
});
stopEditDialog.addEventListener("click", (event) => {
  const choice = event.target.closest("[data-stop-choice]");
  if (choice) applyStopCandidate(Number(choice.dataset.stopChoice));
  else if (event.target === stopEditDialog) {
    const rect = stopEditDialog.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      closeStopEditor();
  }
});
