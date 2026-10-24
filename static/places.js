// 地点资料仅使用高德的实际返回值；历史行程也可以补齐资料。
let placeCatalog = [];
let placeDetails = {};
let placeLoadVersion = 0;

function placeFingerprint(value) {
  let hash = 2166136261;
  for (const char of value)
    hash = Math.imul(hash ^ char.codePointAt(0), 16777619);
  return (hash >>> 0).toString(36);
}

function placeAddress(value) {
  return normalizeAddress(value).replace(/\s/g, "");
}

function findPlace(name, address = "") {
  const matches = placeCatalog.filter((ref) => ref.name === name);
  const target = placeAddress(address);
  const exact = matches.find((ref) => placeAddress(ref.address) === target);
  if (exact) return exact;
  const compatible = matches.filter((ref) => {
    const source = placeAddress(ref.address);
    return (
      source && target && (source.includes(target) || target.includes(source))
    );
  });
  return compatible.length === 1
    ? compatible[0]
    : !target && matches.length === 1
      ? matches[0]
      : null;
}

function preparePlaces(plan) {
  placeLoadVersion++;
  placeCatalog = [];
  placeDetails = activeTrip?.placeDetails || {};
  const add = (name, address, location = {}, poiId = "") => {
    if (!name || findPlace(name, address)) return;
    const ref = {
      name,
      address: normalizeAddress(address),
      poi_id: /^B[A-Z0-9]{8,15}$/.test(poiId) ? poiId : "",
      lat: Number.isFinite(location?.lat) ? location.lat : null,
      lng: Number.isFinite(location?.lng) ? location.lng : null,
    };
    ref.key = `p-${placeFingerprint(JSON.stringify(ref))}-${name.slice(0, 80)}`;
    placeCatalog.push(ref);
  };
  (plan.selected_attractions || []).forEach((p) =>
    add(p.name, p.location?.address, p.location, p.candidate_id),
  );
  const addHotel = (p) => p && add(p.name, p.location?.address, p.location);
  addHotel(plan.recommended_hotel);
  (plan.daily_stays || []).forEach((p) => {
    addHotel(p.start_hotel);
    addHotel(p.end_hotel);
  });
  (plan.hotel_candidates || []).forEach(addHotel);
  (plan.daily_plans || []).forEach((day) =>
    (day.items || [])
      .filter((p) => p.item_type !== "transport")
      .forEach((p) =>
        add(p.location_name || p.title, p.location_address, p.location),
      ),
  );
  if (activeTrip?.mode === "recorded_demo") {
    const recorded = Object.values(activeTrip.placeDetails || {});
    for (const ref of placeCatalog) {
      if (placeDetails[ref.key]) continue;
      const matches = recorded.filter((fact) => fact.name === ref.name);
      const fact =
        matches.find((fact) =>
          placeAddress(fact.address).includes(placeAddress(ref.address)),
        ) || (matches.length === 1 ? matches[0] : null);
      if (fact) placeDetails[ref.key] = { ...fact, key: ref.key };
    }
  }
  // Visit times must not change the identity used by photos/address metadata.
  (plan.daily_plans || []).forEach((day) =>
    (day.items || []).filter(isOpeningStop).forEach((item) => {
      const ref = findPlace(
        item.location_name || item.title,
        item.location_address,
      );
      if (!ref) return;
      ref.visits ||= [];
      if (
        !ref.visits.some(
          (v) => v.date === day.date && v.time_range === item.time_range,
        )
      )
        ref.visits.push({ date: day.date, time_range: item.time_range });
    }),
  );
}

function isOpeningStop(item) {
  return (
    item.item_type === "attraction" ||
    (["meal", "food"].includes(item.item_type) && item.is_route_stop !== false)
  );
}

function openingVisitCheck(item, visitDate, address = item.location_address) {
  const ref = findPlace(item.location_name || item.title, address);
  const matches = (check) =>
    check?.date === visitDate && check.time_range === item.time_range;
  return (
    placeDetails[ref?.key]?.visit_checks?.find(matches) ||
    (matches(item.opening_check) ? item.opening_check : null)
  );
}

function openingHoursContent(check, pending = false) {
  if (!check)
    return `<p class="hours-pending">${pending ? "正在核对开放时间…" : "开放时间待确认"}</p>`;
  const status = ["fits", "conflict", "closed", "unknown"].includes(
    check.status,
  )
    ? check.status
    : "unknown";
  const label =
    status === "unknown"
      ? "开放时间待确认"
      : status === "closed"
        ? "当日不开放"
        : check.hours_text === "24 小时开放"
          ? check.hours_text
          : `开放 ${check.hours_text}`;
  const badge = {
    fits: "时段吻合",
    conflict: "时段冲突",
    closed: "请调整安排",
    unknown: "",
  }[status];
  const queried = check.checked_at ? new Date(check.checked_at) : null;
  const timestamp =
    queried && !Number.isNaN(queried.getTime())
      ? queried.toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        })
      : "";
  return `<details class="place-hours hours-${status}"><summary><span class="hours-label">${escapeHtml(label)}</span>${badge ? `<span class="hours-state">${badge}</span>` : ""}<svg class="icon hours-toggle" aria-hidden="true"><use href="#i-chevron"/></svg></summary><div class="hours-detail"><p>${escapeHtml(check.date)} · 安排 ${escapeHtml(check.time_range)}</p><p>${escapeHtml(check.message)}</p>${check.raw_text ? `<p class="hours-raw">原始规则：${escapeHtml(check.raw_text)}</p>` : ""}${check.source ? `<small>${escapeHtml(check.source)}${timestamp ? ` · 查询于 ${escapeHtml(timestamp)}` : ""}</small>` : ""}<p class="hours-footnote">按常规规则核对，临时调整与预约请向场所确认。</p></div></details>`;
}

function renderOpeningHours(item, dayIndex, address) {
  if (!isOpeningStop(item)) return "";
  const visitDate = activeTrip?.plan.daily_plans[dayIndex - 1]?.date;
  const ref = findPlace(item.location_name || item.title, address);
  const check = openingVisitCheck(item, visitDate, address);
  return `<div class="place-hours-slot" data-place-hours="${escapeHtml(ref?.key || "")}" data-visit-date="${escapeHtml(visitDate || "")}" data-visit-time="${escapeHtml(item.time_range)}">${openingHoursContent(check, true)}</div>`;
}

function fallbackMapUrl(name) {
  const params = new URLSearchParams({
    keyword: `${activeTrip?.plan.city || ""} ${name}`,
    city: activeTrip?.plan.city || "",
    view: "map",
    src: "roam",
  });
  return `https://uri.amap.com/search?${params}`;
}

function safePlaceUrl(value, map = false) {
  try {
    const url = new URL(value);
    if (
      url.username ||
      url.password ||
      !["http:", "https:"].includes(url.protocol)
    )
      return "";
    if (map && (url.protocol !== "https:" || url.hostname !== "uri.amap.com"))
      return "";
    return url.href;
  } catch {
    return "";
  }
}

function renderPlacePhoto(name, address = "", compact = false) {
  const ref = findPlace(name, address);
  const status =
    ref && !placeDetails[ref.key] ? "正在查找照片" : "暂无实景照片";
  return `<figure class="place-photo ${compact ? "compact-photo" : ""}" data-place-photo="${escapeHtml(ref?.key || "")}" aria-label="${escapeHtml(name)}照片"><div class="photo-placeholder"><svg class="icon" aria-hidden="true"><use href="#i-pin"/></svg><span>地点影像</span><small>${status}</small></div></figure>`;
}

function renderPlaceAddress(name, address = "") {
  const ref = findPlace(name, address);
  const details = placeDetails[ref?.key];
  return `<p class="place-address" data-place-address="${escapeHtml(ref?.key || "")}">${escapeHtml(details?.address || address || "详细地址待核实")}</p>`;
}

function renderPlaceMap(name, address = "") {
  const ref = findPlace(name, address);
  const details = placeDetails[ref?.key];
  return `<a class="place-map-link" data-place-map="${escapeHtml(ref?.key || "")}" href="${escapeHtml(safePlaceUrl(details?.map_url, true) || fallbackMapUrl(name))}" target="_blank" rel="noopener noreferrer"><svg class="icon" aria-hidden="true"><use href="#i-pin"/></svg><span>${details?.verified ? "在高德查看" : "在高德搜索"}</span><span aria-hidden="true">↗</span></a>`;
}

function updatePlaceElements() {
  els.resultView.querySelectorAll("[data-place-hours]").forEach((slot) => {
    const checks = placeDetails[slot.dataset.placeHours]?.visit_checks;
    const check = checks?.find(
      (v) =>
        v.date === slot.dataset.visitDate &&
        v.time_range === slot.dataset.visitTime,
    );
    if (!check) return;
    const signature = JSON.stringify(check);
    if (slot.dataset.hoursSignature === signature) return;
    const expanded = slot.querySelector("details")?.open;
    const focused = slot.contains(document.activeElement);
    slot.innerHTML = openingHoursContent(check);
    if (expanded) slot.querySelector("details").open = true;
    if (focused) slot.querySelector("summary").focus({ preventScroll: true });
    slot.dataset.hoursSignature = signature;
  });
  els.resultView.querySelectorAll("[data-place-address]").forEach((el) => {
    const detail = placeDetails[el.dataset.placeAddress];
    if (detail?.address) el.textContent = detail.address;
  });
  els.resultView.querySelectorAll("[data-place-map]").forEach((el) => {
    const detail = placeDetails[el.dataset.placeMap];
    const url = safePlaceUrl(detail?.map_url, true);
    if (!url) return;
    el.href = url;
    el.querySelector("span").textContent = detail.verified
      ? "在高德查看"
      : "在高德搜索";
    el.title = detail.verified
      ? `高德地点：${detail.address}`
      : "地点未唯一匹配，请在高德确认具体位置";
  });
  els.resultView.querySelectorAll("[data-place-photo]").forEach((el) => {
    const detail = placeDetails[el.dataset.placePhoto];
    const url = safePlaceUrl(detail?.photo_url);
    const status = el.querySelector(".photo-placeholder small");
    if (!url) {
      if (detail) {
        el.querySelectorAll("img, figcaption").forEach((child) =>
          child.remove(),
        );
        el.classList.remove("photo-loaded");
        delete el.dataset.photoUrl;
        status.textContent = "暂无实景照片";
      }
      return;
    }
    if (el.dataset.photoUrl === url) return;
    el.querySelectorAll("img, figcaption").forEach((child) => child.remove());
    el.classList.remove("photo-loaded");
    el.dataset.photoUrl = url;
    status.textContent = "正在加载照片";
    const img = new Image();
    img.alt = `${detail.name}的地点照片`;
    img.loading = "lazy";
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    img.onload = () => {
      el.classList.add("photo-loaded");
    };
    img.onerror = () => {
      img.remove();
      el.classList.remove("photo-loaded");
      status.textContent = "照片暂时无法加载";
    };
    const caption = document.createElement("figcaption");
    caption.textContent = detail.photo_source;
    el.append(img, caption);
    img.src = url;
  });
}

async function loadPlaceDetails(plan) {
  if (activeTrip?.mode === "recorded_demo") {
    updatePlaceElements();
    return;
  }
  const version = placeLoadVersion;
  const refs = placeCatalog.slice();
  const trip = activeTrip;
  updatePlaceElements();
  for (let offset = 0; offset < refs.length; offset += 12) {
    if (version !== placeLoadVersion || activeTrip?.plan !== plan) return;
    try {
      const response = await fetch("/api/places/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          city: plan.city,
          places: refs.slice(offset, offset + 12),
        }),
      });
      if (!response.ok) continue;
      const data = await response.json();
      if (version !== placeLoadVersion || activeTrip?.plan !== plan) return;
      (data.places || []).forEach((p) => {
        placeDetails[p.key] = p;
      });
      trip.placeDetails = { ...placeDetails };
      updatePlaceElements();
    } catch {
      /* 资料暂不可用时保留原地址、高德搜索与明确的照片占位。 */
    }
  }
  if (version === placeLoadVersion && trip === activeTrip) {
    els.resultView.querySelectorAll(".hours-pending").forEach((el) => {
      el.textContent = "开放时间待确认";
    });
    els.resultView.querySelectorAll("[data-place-photo]").forEach((el) => {
      if (!placeDetails[el.dataset.placePhoto])
        el.querySelector(".photo-placeholder small").textContent =
          "暂未获取照片";
    });
    // Persist only metadata if the stored itinerary is still this version.
    persistTripMetadata(trip);
  }
}
