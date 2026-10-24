// One pending operation per tab. SQLite owns execution; the browser owns display.
const pendingTaskStorageKey = "roam.pending-task.v1";
let volatilePendingTask = null;

function readPendingTask() {
  try {
    const value = JSON.parse(
      sessionStorage.getItem(pendingTaskStorageKey) || "null",
    );
    return value?.key &&
      ["plan", "revision", "replacement"].includes(value.kind)
      ? value
      : volatilePendingTask;
  } catch {
    return volatilePendingTask;
  }
}

function writePendingTask(value) {
  volatilePendingTask = value;
  try {
    if (value)
      sessionStorage.setItem(pendingTaskStorageKey, JSON.stringify(value));
    else sessionStorage.removeItem(pendingTaskStorageKey);
  } catch {
    /* A live tab still works if session storage is full. */
  }
}

function completeBackgroundTask(taskId) {
  if (readPendingTask()?.taskId === taskId) writePendingTask(null);
  document.querySelector("#taskRecoveryBanner").hidden = true;
}

function saveBackgroundResult(taskId) {
  const pending = readPendingTask();
  const current = readSavedTrips();
  if (pending && pending.kind !== "plan" && activeTrip.id === pending.tripId) {
    const newer = current.find((trip) => trip.id === pending.tripId);
    if (
      newer &&
      (newer.updatedAt || newer.createdAt) !== pending.baseVersion &&
      newer.lastTaskId !== taskId
    ) {
      activeTrip.id = pending.outputId;
      activeTrip.createdAt = Date.now();
      showToast("原行程已在其他标签页更新，本次结果已另存一份");
    }
  }
  activeTrip.lastTaskId = taskId;
  if (saveTrip(activeTrip)) {
    completeBackgroundTask(taskId);
    return true;
  }
  showTaskRecoveryBanner();
  return false;
}

function showTaskRecoveryBanner() {
  document.querySelector("#resumeTaskBtn").hidden = false;
  document.querySelector("#taskRecoveryBanner").hidden = !readPendingTask();
}

async function postBackgroundTask(kind, payload, { message = "" } = {}) {
  if (readPendingTask()) {
    showTaskRecoveryBanner();
    throw {
      detail: "还有一项提交等待查看，请先点击“继续查看”。",
      recoverable: true,
    };
  }
  const context = {
    key: crypto.randomUUID(),
    kind,
    taskId: null,
    tripId: activeTrip?.id || null,
    baseVersion: activeTrip?.updatedAt || activeTrip?.createdAt || null,
    outputId: crypto.randomUUID(),
    message,
    createdAt: Date.now(),
  };
  writePendingTask(context);
  const endpoint = {
    plan: "/api/plan/tasks",
    revision: "/api/plan/revisions/tasks",
    replacement: "/api/plan/places/replace/tasks",
  }[kind];
  for (let attempt = 0; attempt < 2; attempt++) {
    let response;
    try {
      response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": context.key,
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(15000),
      });
    } catch {
      if (attempt === 0) continue;
      showTaskRecoveryBanner();
      throw {
        detail: "暂时无法确认提交状态，请点击“继续查看”恢复，避免重复生成。",
        recoverable: true,
      };
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      // A gateway/server failure may occur after the task was committed.
      if (response.status >= 500) {
        if (attempt === 0) continue;
        showTaskRecoveryBanner();
        throw {
          detail: "服务暂未确认提交结果，请点击“继续查看”，避免重复生成。",
          recoverable: true,
        };
      }
      writePendingTask(null);
      throw data.detail ? data : { detail: "提交未完成，请稍后重试。" };
    }
    if (!data.task_id) {
      showTaskRecoveryBanner();
      throw {
        detail: "任务响应不完整，请点击“继续查看”确认提交结果。",
        recoverable: true,
      };
    }
    context.taskId = data.task_id;
    writePendingTask(context);
    return { ...data, output_id: context.outputId };
  }
}

function friendlyTaskProgress(data) {
  if (!/Agent|MCP|LLM|JSON/.test(`${data.title} ${data.detail}`)) return data;
  const labels = {
    research_agents: [
      "查找景点、天气与住宿",
      "正在结合你的偏好核对可用地点与出行资料。",
    ],
    itinerary: [
      "编排每日安排",
      "正在安排景点、三餐和住宿，留出合适的停留时间。",
    ],
    meal: ["完善用餐安排", "正在查找沿途合适的餐厅并核对地点。"],
  };
  const [title, detail] = labels[data.stage] || [
    "核对旅行安排",
    "正在核对候选地点与旅行需求。",
  ];
  return { ...data, title, detail };
}

function watchBackgroundTask(taskId, { revision = false, onProgress } = {}) {
  return new Promise((resolve, reject) => {
    let settled = false,
      reconciling = false,
      disconnected = false;
    let lastEventAt = Date.now(),
      unreachableSince = null;
    const source = new EventSource(
      `/api/plan/stream/${encodeURIComponent(taskId)}`,
    );
    progressSource = source;
    const progress = (data) => {
      const value = friendlyTaskProgress(data);
      if (revision) onProgress?.(value);
      else setProgress(value);
    };
    const close = () => {
      source.close();
      clearInterval(monitor);
      if (progressSource === source) progressSource = null;
    };
    const finish = (data, error = false, recoverable = false) => {
      if (settled) return;
      settled = true;
      close();
      if (error) {
        if (!recoverable) completeBackgroundTask(taskId);
        else showTaskRecoveryBanner();
        if (!revision) failProgress();
        reject({ ...data, recoverable });
      } else {
        if (!revision) completeProgress();
        resolve(revision ? data.revision : data.plan);
      }
    };
    async function reconcile() {
      if (settled || reconciling) return;
      reconciling = true;
      try {
        const response = await fetch(
          `/api/plan/tasks/${encodeURIComponent(taskId)}`,
          { signal: AbortSignal.timeout(8000) },
        );
        const task = await response.json();
        if (!response.ok) {
          finish(task, true);
          return;
        }
        unreachableSince = null;
        if (task.status === "completed") finish(task.result);
        else if (task.status === "failed") finish(task.error, true);
      } catch {
        unreachableSince ||= Date.now();
        if (Date.now() - unreachableSince >= 60000)
          finish(
            { detail: "连接暂未恢复，任务仍可稍后继续查看；原行程已保留。" },
            true,
            true,
          );
      } finally {
        reconciling = false;
      }
    }
    const monitor = setInterval(() => {
      if (disconnected || Date.now() - lastEventAt > 45000) void reconcile();
    }, 5000);
    source.addEventListener("progress", (event) => {
      lastEventAt = Date.now();
      disconnected = false;
      unreachableSince = null;
      const data = parseSsePayload(event.data);
      if (data) progress(data);
    });
    source.addEventListener("complete", (event) =>
      finish(parseSsePayload(event.data) || {}),
    );
    source.addEventListener("failed", (event) =>
      finish(
        parseSsePayload(event.data) || { detail: "这次任务未能完成。" },
        true,
      ),
    );
    source.onerror = () => {
      if (settled) return;
      disconnected = true;
      progress({
        stage: "reconnecting",
        title: "正在恢复连接",
        detail: "后台继续处理，正在重新获取进度与结果。",
      });
      void reconcile();
    };
  });
}

function openRecoveredTrip(trip) {
  activeTrip = trip;
  applyRequest(trip.request);
  showPage("result");
  renderPlan(trip.plan, trip.request);
  renderHistory();
  setState("已恢复");
}

async function restorePendingTask() {
  const pending = readPendingTask();
  if (!pending || isPlanning) return;
  isPlanning = true;
  setNavigationBusy(true);
  document.querySelector("#resumeTaskBtn").disabled = true;
  const bannerMessage = document.querySelector("#taskRecoveryMessage");
  bannerMessage.textContent = "正在恢复上次提交的进度与结果…";
  showTaskRecoveryBanner();
  try {
    const url = pending.taskId
      ? `/api/plan/tasks/${encodeURIComponent(pending.taskId)}`
      : `/api/plan/submissions/${encodeURIComponent(pending.key)}`;
    const response = await fetch(url, { signal: AbortSignal.timeout(10000) });
    const snapshot = await response.json();
    if (!response.ok) {
      if (response.status === 404) writePendingTask(null);
      throw snapshot;
    }
    pending.taskId = snapshot.id;
    writePendingTask(pending);
    let trip = savedTrips.find((item) => item.lastTaskId === snapshot.id);
    if (trip) {
      openRecoveredTrip(trip);
      completeBackgroundTask(snapshot.id);
      return;
    }
    if (snapshot.status === "failed") {
      writePendingTask(null);
      throw snapshot.error;
    }
    const request =
      pending.kind === "plan" ? snapshot.payload : snapshot.payload.request;
    if (pending.kind === "plan") {
      activeTrip = null;
      applyRequest(request);
      resetResultView();
      showPage("result");
      startProgress();
      document.querySelector("#resultHeading").textContent =
        `${request.city} · 正在恢复规划`;
      document.querySelector("#tripDateLabel").textContent =
        `${request.start_date} — ${request.end_date}`;
    } else {
      trip = savedTrips.find((item) => item.id === pending.tripId);
      // Another tab may have edited this trip. Preserve that version and restore a copy.
      if (!trip || (trip.updatedAt || trip.createdAt) !== pending.baseVersion) {
        trip = {
          id: pending.outputId,
          createdAt: Date.now(),
          request,
          plan: snapshot.payload.plan,
          conversation: snapshot.payload.history || [],
          revisions: [],
          dayRules: snapshot.payload.day_rules || {},
          revisionCount: 0,
        };
      }
      activeTrip = structuredClone(trip);
      applyRequest(request);
      showPage("result");
      renderPlan(activeTrip.plan, activeTrip.request);
    }
    let result;
    if (snapshot.status === "completed")
      result =
        pending.kind === "plan"
          ? snapshot.result.plan
          : snapshot.result.revision;
    else
      result = await watchBackgroundTask(snapshot.id, {
        revision: pending.kind !== "plan",
        onProgress: (data) => {
          bannerMessage.textContent = `${data.title} · ${data.detail}`;
        },
      });
    if (pending.kind === "plan") {
      if (!result?.daily_plans)
        throw { detail: "未获取到完整行程，可以稍后继续查看。" };
      activeTrip = {
        id: pending.outputId,
        createdAt: Date.now(),
        request,
        plan: result,
        conversation: [],
        revisions: [],
        dayRules: {},
        revisionCount: 0,
      };
    } else {
      if (!result?.plan)
        throw { detail: "未获取到完整修改结果，原行程已保留。" };
      const previous = revisionSnapshot(activeTrip);
      const message =
        pending.message || snapshot.payload.message || "更换一个地点。";
      activeTrip.conversation = [
        ...(activeTrip.conversation || []),
        { role: "user", content: message },
      ];
      commitTripRevision(result, previous);
    }
    saveBackgroundResult(snapshot.id);
    openRecoveredTrip(activeTrip);
    completeProgress();
    showToast("已恢复上次提交的结果");
  } catch (error) {
    bannerMessage.textContent =
      extractErrorDetail(error) || "暂时无法恢复，可以稍后再试。";
    document.querySelector("#taskRecoveryBanner").hidden = false;
    if (!readPendingTask())
      document.querySelector("#resumeTaskBtn").hidden = true;
  } finally {
    isPlanning = false;
    setNavigationBusy(false);
    setRevisionBusy(false);
    document.querySelector("#resumeTaskBtn").disabled = false;
    updateDateSpanValidity();
  }
}

function initializeTaskRecovery() {
  document.querySelector("#resumeTaskBtn").onclick = restorePendingTask;
  document.querySelector("#dismissTaskRecovery").onclick = () => {
    document.querySelector("#taskRecoveryBanner").hidden = true;
  };
  if (readPendingTask()) void restorePendingTask();
}
