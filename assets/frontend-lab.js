/* Presentation experiment over the existing dashboard's data adapters and map. */
(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const safe = (value) => escapeHTML(value);
  const paths = {
    mark: '<path d="M18 3 31 10.5v15L18 33 5 25.5v-15Z"/><path d="m5 10.5 13 8 13-8M18 18.5V33M11.5 6.8l13 7.5v7.5"/>',
    monitor: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M14 18h7m-3.5-3.5v7"/>',
    briefing: '<path d="M6 3h9l4 4v14H6Z"/><path d="M14 3v5h5M9 12h7m-7 4h7"/>',
    scenario: '<path d="M8 3h8m-6 0v6L4 19a1.3 1.3 0 0 0 1 2h14a1.3 1.3 0 0 0 1-2L14 9V3M7 15h10"/>',
    plan: '<path d="m12 3 8 3v6c0 4-5 8-8 9-3-1-8-5-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    pin: '<path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2.5"/>',
    layers: '<path d="m12 3 10 6-10 6L2 9Zm-10 11 10 6 10-6M2 19l10 6 10-6" transform="translate(0 -1) scale(1 .9)"/>',
    alert: '<path d="m12 3 10 18H2Z"/><path d="M12 9v5m0 3h.01"/>',
    people: '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v2"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
    globe: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
    expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
    refresh: '<path d="M20 8a8 8 0 1 0 .3 7M20 3v6h-6"/>',
  };
  const icon = (name) => `<svg class="lab-icon" viewBox="0 0 ${name === "mark" ? "36 36" : "24 24"}" aria-hidden="true">${paths[name] || paths.info}</svg>`;
  const node = (markup) => {
    const template = document.createElement("template");
    template.innerHTML = markup.trim();
    return template.content.firstElementChild;
  };
  const metric = (title, id, symbol, note, extra = "") => `
    <article class="lab-metric ${extra}">
      <div class="lab-metric-top"><span>${title}</span>${icon(symbol)}</div>
      <div class="lab-metric-value"><strong id="${id}">—</strong><span id="${id}Unit"></span></div>
      <small id="${id}Note">${note}</small>
    </article>`;

  document.body.classList.add("frontend-lab");
  byId("t-sit").textContent = "Situation overview";
  document.body.prepend(node('<a class="lab-skip" href="#labMain">Skip to workspace</a>'));

  // Preserve every original tab and control, including their event handlers.
  const rail = node(`<aside class="lab-rail" aria-label="Workspace navigation">
    <div class="lab-mark" aria-label="ClimateGuard">${icon("mark")}</div>
    <div class="lab-rail-bottom"><span>CAN</span></div>
  </aside>`);
  const nav = document.querySelector("nav.tabs");
  const navCopy = {
    situation: ["monitor", "Monitor"],
    briefing: ["briefing", "Briefing"],
    whatif: ["scenario", "Scenarios"],
    plan: ["plan", "Plan review"],
  };
  nav.querySelectorAll(".tab").forEach((button) => {
    const [symbol, label] = navCopy[button.dataset.tab];
    button.innerHTML = `${icon(symbol)}<span>${label}</span>`;
    button.id = `labNav-${button.dataset.tab}`;
    button.setAttribute("aria-controls", `v-${button.dataset.tab}`);
    byId(`v-${button.dataset.tab}`).setAttribute("role", "tabpanel");
    byId(`v-${button.dataset.tab}`).setAttribute("aria-labelledby", button.id);
  });
  rail.insertBefore(nav, rail.lastElementChild);
  document.body.prepend(rail);
  nav.querySelector(".tabs-inner").setAttribute("aria-orientation", "vertical");

  const brand = document.querySelector(".brand");
  brand.querySelector("h1").textContent = "ClimateGuard";
  brand.querySelector("p").textContent = "Climate & emergency intelligence";
  const clock = document.querySelector(".clock");
  const headerRight = node(`<div class="lab-header-right">
    <div class="lab-feed" id="labFeed" data-state="cached"><span class="lab-dot"></span><span id="labFeedLabel">Checking source coverage</span></div>
    <div class="lab-palette" role="group" aria-label="Workspace colour palette">
      <button type="button" data-palette="forest" aria-label="Forest green palette" aria-pressed="true"><span class="lab-swatch"></span>Forest</button>
      <button type="button" data-palette="ink" aria-label="Ink and teal palette" aria-pressed="false"><span class="lab-swatch ink"></span>Ink & teal</button>
    </div>
  </div>`);
  document.querySelector(".top-inner").append(headerRight);

  const main = document.querySelector("main");
  main.id = "labMain";
  const situation = byId("v-situation");
  const intro = node(`<div class="lab-intro">
    <div><div class="lab-kicker">Canada <span>/</span> Operations workspace</div>
    <h2>Situation overview<span style="color:var(--moss)">.</span></h2>
    <p>Understand the conditions. Focus the response.</p></div>
    <div class="lab-intro-actions">
      <span class="lab-mode" id="labMode"><span class="lab-dot"></span><span>Saved assessments</span></span>
      <button class="btn" type="button" id="labOpenBriefing">${icon("briefing")}Open briefing${icon("arrow")}</button>
    </div>
  </div>`);
  situation.insertBefore(intro, byId("summary"));
  const metrics = node(`<div class="lab-metrics" aria-label="Data coverage and preparedness context">
    ${metric("Regional coverage", "labCoverage", "globe", "Configured regions with saved assessments")}
    ${metric("Elevated assessments", "labElevated", "alert", "High or critical · saved snapshots", "lab-metric-attention")}
    ${metric("Population context", "labPopulation", "people", "Annual estimate · not hazard exposure")}
    ${metric("Snapshot currency", "labCurrency", "clock", "Checking assessment timestamps")}
  </div>`);
  situation.insertBefore(metrics, byId("summary"));

  const priority = node(`<aside class="lab-priority" aria-labelledby="labPriorityTitle">
    <div class="lab-panel-head"><div><h3 id="labPriorityTitle">Regional watch</h3><p>Review the highest assessments first</p></div><span class="lab-count" id="labRegionCount">—</span></div>
    <div class="lab-priority-tools">
      <label class="lab-search">${icon("search")}<input id="labRegionSearch" type="search" placeholder="Find a region…" aria-label="Filter regional assessments" /></label>
      <div class="lab-priority-sort"><span>Highest score first</span><select id="labRiskFilter" aria-label="Filter assessments by hazard"><option value="all">All hazards</option><option value="wildfire">Wildfire</option><option value="rain">Rain & flooding</option><option value="hurricane">Storm</option></select></div>
    </div>
    <ol class="lab-region-list" id="labRegions" aria-label="Regional risk assessments"><li class="lab-empty">Loading saved assessments…</li></ol>
    <div class="lab-priority-foot">Scores are screening estimates.<br><button type="button" id="labCoverageLink">Review source coverage ${"↗"}</button></div>
  </aside>`);
  document.querySelector(".grid-sit").prepend(priority);

  // A focused inspector replaces the original stack of unrelated panels.
  const side = document.querySelector(".side");
  const communityPanel = byId("communityPanelTitle").closest(".panel");
  const inspector = node(`<section class="lab-inspector" aria-labelledby="labInspectorTitle">
    <div class="lab-inspector-head">${icon("pin")}<h3 id="labInspectorTitle">Regional assessment</h3><span id="labInspectorContext">Snapshot</span></div>
    <div class="lab-detail-tabs" role="tablist" aria-label="Assessment details">
      <button type="button" role="tab" id="labAssessmentTab" data-detail="Assessment" aria-selected="true" aria-controls="labAssessment">Assessment</button>
      <button type="button" role="tab" id="labResourcesTab" data-detail="Resources" aria-selected="false" aria-controls="labResources" tabindex="-1">Resources</button>
      <button type="button" role="tab" id="labEvidenceTab" data-detail="Evidence" aria-selected="false" aria-controls="labEvidence" tabindex="-1">Evidence</button>
    </div>
    <div class="lab-inspector-body">
      <div id="labAssessment" role="tabpanel" aria-labelledby="labAssessmentTab"></div>
      <div id="labResources" role="tabpanel" aria-labelledby="labResourcesTab" hidden><p class="lab-replay-only" id="labReplayResourceNote" hidden>Reception centres and hospitals for this replay are listed below the map.</p></div>
      <div class="lab-evidence" id="labEvidence" role="tabpanel" aria-labelledby="labEvidenceTab" hidden>
        <h4>Assessment provenance</h4><p id="labEvidenceSummary"></p>
        <dl><dt>Assessment generated</dt><dd id="labEvidenceGenerated">—</dd><dt>Valid until</dt><dd id="labEvidenceExpiry">—</dd><dt>Source information</dt><dd id="labEvidenceSources"></dd></dl>
        <button class="btn ghost" type="button" id="labEvidenceLayers">${icon("layers")}Inspect layers & sources</button>
      </div>
    </div>
  </section>`);
  // The inspector is still detached, so query its panels directly.
  const assessmentPanel = inspector.querySelector("#labAssessment");
  ["placeCard", "replayBrief", "areaPanel"].forEach((id) => assessmentPanel.append(byId(id)));
  assessmentPanel.append(node(`<div class="lab-context-action"><button class="btn ghost" type="button" id="labAssessmentBriefing">${icon("briefing")}View preparedness briefing${icon("arrow")}</button></div>`));
  inspector.querySelector("#labResources").append(byId("resourcePanel"));
  side.append(inspector);

  // Details remain available, but don't compete with the map for space.
  function makeDialog(id, title, subtitle) {
    const dialog = node(`<dialog class="lab-dialog" id="${id}" aria-labelledby="${id}Title">
      <div class="lab-dialog-head"><div><h2 id="${id}Title">${title}</h2><p>${subtitle}</p></div><button class="lab-close" type="button" aria-label="Close ${title.toLowerCase()}">${icon("close")}</button></div>
      <div class="lab-dialog-body"></div>
    </dialog>`);
    dialog.querySelector(".lab-close").onclick = () => dialog.close();
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
    document.body.append(dialog);
    return dialog.querySelector(".lab-dialog-body");
  }
  const layerBody = makeDialog("labLayers", "Map layers & sources", "Choose the evidence on your map. Each source retains its status and timestamp.");
  layerBody.append(node('<p class="lab-kicker">Reference layers</p>'));
  layerBody.append(document.querySelector(".overview-layers"));
  layerBody.append(node('<p id="labReplayLayersNote" class="hint" hidden>Replay overlays are controlled above the map. Historical report times and source citations appear in the replay briefing.</p>'));
  layerBody.append(byId("populationLegend"), byId("liveLayers"));
  const watchDetails = node('<details><summary>Canada-wide community screening</summary></details>');
  watchDetails.append(byId("watchPanel"));
  layerBody.append(watchDetails);

  const populationBody = makeDialog("labCommunities", "Community context", "Annual population estimates and geographic context, separate from hazard exposure.");
  populationBody.append(clock, byId("summary"), communityPanel);
  const scenarioBody = makeDialog("labSensitivity", "Sensitivity explorer", "Adjust a planning assumption against the selected regional assessment.");
  scenarioBody.append(node('<p style="font-size:12px;color:var(--ink-2)">This control changes the displayed screening score. It does not forecast hazard behaviour.</p>'), byId("sevBar"));

  const mapHead = document.querySelector(".map-head");
  const headingRow = node('<div class="lab-map-heading"></div>');
  headingRow.append(document.querySelector(".mh-title"));
  headingRow.append(node(`<button class="btn ghost" type="button" id="labLayersButton">${icon("layers")}Layers & sources</button>`));
  mapHead.prepend(headingRow);
  const mapFoot = node(`<div class="lab-map-foot"><span id="labMapScope">Canada · regional assessment locations</span><button type="button" id="labResetMap">${"↗"} Fit Canada</button></div>`);
  document.querySelector(".mapcard").append(mapFoot);
  // Read the context below in the drawer; keep the map header short.
  byId("mapSub").hidden = true;

  situation.append(node(`<div class="lab-note">${icon("info")}<div><b>Planning context.</b> Snapshot age and source availability are shown explicitly. Population is an annual estimate; facility locations do not confirm operating status or capacity.</div></div>`));
  main.append(node(`<div class="lab-bottom"><span>ClimateGuard <span aria-hidden="true">/</span> Government preparedness & response <span aria-hidden="true">·</span> Design preview</span><span class="lab-shortcut"><kbd>/</kbd> Find a region <span aria-hidden="true">·</span> <a href="ClimateGuard_Mockup.html">Original dashboard ${"↗"}</a></span></div>`));

  const workspaceCopy = {
    briefing: ["Decision support", "Preparedness briefing", "Review the situation, supporting evidence and recommended actions before sharing."],
    whatif: ["Scenario workspace", "Explore a change in conditions", "Compare an illustrative wildfire scenario with its baseline, then review the implications."],
    plan: ["Preparedness review", "Put the plan to the test", "Check a sample response plan against the baseline or an illustrative scenario."],
  };
  Object.entries(workspaceCopy).forEach(([tab, [kicker, title, copy]]) => {
    byId(`v-${tab}`).prepend(node(`<div class="lab-workspace-intro"><div class="lab-kicker">${kicker}</div><h2>${title}</h2><p>${copy}</p></div>`));
  });

  // Accessible arrow-key navigation for both tab groups.
  function wireTabKeys(buttons, vertical = false) {
    buttons.forEach((button, index) => button.addEventListener("keydown", (event) => {
      const nextKey = vertical ? "ArrowDown" : "ArrowRight";
      const previousKey = vertical ? "ArrowUp" : "ArrowLeft";
      let target;
      if (event.key === nextKey) target = (index + 1) % buttons.length;
      if (event.key === previousKey) target = (index - 1 + buttons.length) % buttons.length;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = buttons.length - 1;
      if (target === undefined) return;
      event.preventDefault(); buttons[target].click(); buttons[target].focus();
    }));
  }
  const detailButtons = [...inspector.querySelectorAll("[data-detail]")];
  function selectDetail(name) {
    detailButtons.forEach((button) => {
      const active = button.dataset.detail === name;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      byId(`lab${button.dataset.detail}`).hidden = !active;
    });
  }
  detailButtons.forEach((button) => button.onclick = () => selectDetail(button.dataset.detail));
  wireTabKeys(detailButtons);
  const navigationButtons = [...nav.querySelectorAll(".tab")];
  wireTabKeys(navigationButtons, true);
  function syncNavigation() {
    navigationButtons.forEach((button) => {
      button.tabIndex = button.getAttribute("aria-selected") === "true" ? 0 : -1;
    });
  }
  navigationButtons.forEach((button) => button.addEventListener("click", syncNavigation));
  syncNavigation();

  const openLayers = () => byId("labLayers").showModal();
  ["labLayersButton", "labEvidenceLayers", "labCoverageLink"].forEach((id) => byId(id).onclick = openLayers);
  const openBriefing = () => { showTab("briefing"); syncNavigation(); };
  ["labOpenBriefing", "labAssessmentBriefing"].forEach((id) => byId(id).onclick = openBriefing);
  byId("labResetMap").onclick = () => {
    if (mode === "detail") exitDetail();
    map?.flyToBounds(CANADA_BOUNDS, { duration: reduced ? 0 : .7 });
  };

  let palette = "forest";
  try { palette = localStorage.getItem("climateguard-lab-palette") === "ink" ? "ink" : "forest"; } catch { /* Private browsing can deny storage. */ }
  function setPalette(value) {
    document.documentElement.dataset.palette = value;
    headerRight.querySelectorAll("[data-palette]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.palette === value)));
    try { localStorage.setItem("climateguard-lab-palette", value); } catch { /* Palette still works without storage. */ }
  }
  headerRight.querySelectorAll("[data-palette]").forEach((button) => button.onclick = () => setPalette(button.dataset.palette));
  setPalette(palette);

  const hazardName = { wildfire: "Wildfire", rain: "Rain & flooding", hurricane: "Storm" };
  const uiHazard = { wildfire: "wildfire", flood: "rain", severe_storm: "hurricane" };
  let listSignature = "";
  function renderRegionalList() {
    const filter = byId("labRiskFilter").value;
    const query = byId("labRegionSearch").value.trim().toLocaleLowerCase();
    const entries = dataAdapter.riskIndex?.entries || [];
    const cards = REGIONS.map((region) => {
      const matches = entries.filter((entry) => entry.regionId === region.id && uiHazard[entry.hazard] && (filter === "all" || uiHazard[entry.hazard] === filter));
      const best = matches.sort((a, b) => (b.score ?? -1) - (a.score ?? -1))[0];
      return { region, entry: best };
    }).filter(({ region }) => !query || `${region.name} ${region.sub}`.toLocaleLowerCase().includes(query))
      .sort((a, b) => (b.entry?.score ?? -1) - (a.entry?.score ?? -1) || a.region.name.localeCompare(b.region.name));
    const signature = JSON.stringify([cards, prov, hazard, mode]);
    if (signature === listSignature) return;
    listSignature = signature;
    byId("labRegionCount").textContent = String(cards.length).padStart(2, "0");
    byId("labRegions").innerHTML = cards.length ? cards.map(({ region, entry }) => {
      const hz = entry ? uiHazard[entry.hazard] : filter === "all" ? hazard : filter;
      const stale = entry?.expiresAt && Date.now() > Date.parse(entry.expiresAt);
      const level = entry?.level;
      const labels = { low: ["Low", "b-low"], moderate: ["Moderate", "b-mod"], high: ["High", "b-high"], critical: ["Critical", "b-ext"], extreme: ["Extreme", "b-ext"] };
      const [label, bandClass] = labels[level] || ["No assessment", ""];
      const selected = region.id === prov && hz === hazard;
      return `<li><button class="lab-region" type="button" data-region="${safe(region.id)}" data-hazard="${safe(hz)}" aria-pressed="${selected}" aria-label="${safe(region.name)}, ${safe(hazardName[hz])}, ${safe(label)}${stale ? ", stale snapshot" : ""}">
        <span class="lab-region-title"><b>${safe(region.name.split(",")[0])}</b><small>${safe(region.name.split(",")[1]?.trim() || "")}</small></span>
        <span class="lab-region-meta"><span>${safe(entry ? hazardName[hz] : region.sub)}</span><span class="band ${bandClass}">${safe(label)}${Number.isFinite(entry?.score) ? ` · ${entry.score}` : ""}</span></span>
        <span class="lab-region-sub">${entry ? `${stale ? "Stale snapshot" : "Saved snapshot"} · ${safe(formatSnapshotTime(entry.generatedAt))}` : "Awaiting a regional snapshot"}</span>
      </button></li>`;
    }).join("") : '<li class="lab-empty">No region matches your search.</li>';
    byId("labRegions").querySelectorAll("[data-region]").forEach((button) => button.onclick = () => {
      if (mode === "detail") exitDetail();
      const hzButton = document.querySelector(`.hz button[data-hazard="${button.dataset.hazard}"]`);
      if (button.dataset.hazard !== hazard) hzButton?.click();
      selectProv(button.dataset.region);
      const region = REGIONS.find((item) => item.id === button.dataset.region);
      map?.flyTo([region.lat, region.lon], 6, { duration: reduced ? 0 : .7 });
      selectDetail("Assessment");
      scheduleSync();
      if (matchMedia("(max-width: 760px)").matches) inspector.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
    });
  }
  byId("labRegionSearch").oninput = renderRegionalList;
  byId("labRiskFilter").onchange = renderRegionalList;
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey || document.querySelector("dialog[open]") || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName) || event.target.isContentEditable) return;
    event.preventDefault();
    if (byId("v-situation").hidden) { showTab("situation"); syncNavigation(); }
    byId("labRegionSearch").focus();
  });

  let syncFrame = 0;
  function scheduleSync() {
    if (syncFrame) return;
    syncFrame = requestAnimationFrame(() => { syncFrame = 0; syncWorkspace(); });
  }
  function syncWorkspace() {
    const entries = dataAdapter.riskIndex?.entries || [];
    const knownRegions = new Set(REGIONS.map((region) => region.id));
    const regionalEntries = entries.filter((entry) => knownRegions.has(entry.regionId));
    const regionCount = new Set(regionalEntries.map((entry) => entry.regionId)).size;
    const highRegions = new Set(regionalEntries.filter((entry) => ["high", "critical", "extreme"].includes(entry.level)).map((entry) => entry.regionId)).size;
    const staleCount = regionalEntries.filter((entry) => !entry.expiresAt || Date.now() > Date.parse(entry.expiresAt)).length;
    byId("labCoverage").textContent = String(regionCount).padStart(2, "0");
    byId("labCoverageUnit").textContent = `/ ${REGIONS.length} regions`;
    byId("labCoverageNote").textContent = `${regionalEntries.length} saved hazard assessments`;
    byId("labElevated").textContent = String(highRegions).padStart(2, "0");
    byId("labElevatedUnit").textContent = highRegions === 1 ? "region" : "regions";
    byId("labCurrency").textContent = regionalEntries.length ? `${regionalEntries.length - staleCount}/${regionalEntries.length}` : "—";
    byId("labCurrencyUnit").textContent = "within validity";
    byId("labCurrencyNote").textContent = staleCount ? `${staleCount} expired · refresh before operational use` : regionalEntries.length ? "Based on assessment expiry timestamps" : "No regional snapshot index available";
    const metadata = dataAdapter.population.metadata;
    byId("labPopulation").textContent = metadata?.totalPopulation ? `${(metadata.totalPopulation / 1e6).toFixed(2)}M` : "—";
    byId("labPopulationUnit").textContent = "Canada";
    byId("labPopulationNote").textContent = metadata ? `July 1, ${metadata.year} estimate · not hazard exposure` : "Annual estimate · loading population context";

    const live = dataAdapter.live;
    const currentSources = (live.manifest?.layers || []).filter((entry) => liveLayerStatus(entry.id)[0] === "live").length;
    const isLive = currentSources > 0;
    byId("labFeed").dataset.state = isLive ? "live" : "cached";
    byId("labFeedLabel").textContent = isLive ? `${currentSources} current live source layers` : live.manifest ? "Source status in layer drawer" : "Saved snapshots · live layers unavailable";
    const replayMode = mode === "detail";
    const modeBadge = byId("labMode");
    modeBadge.dataset.mode = replayMode ? "replay" : severity !== 0 ? "scenario" : "snapshot";
    modeBadge.lastElementChild.textContent = replayMode ? "Historical replay" : severity !== 0 ? "Sensitivity scenario" : "Saved assessments";
    byId("labMapScope").textContent = replayMode ? "Historical fire reports · replay timeline" : `${hazardName[hazard]} · regional assessment locations`;
    byId("mapTitle").textContent = replayMode ? `${REGIONS.find((region) => region.id === prov)?.name || "Wildfire"} · replay` : "National situation map";
    byId("labInspectorContext").textContent = replayMode ? "Replay" : "Snapshot";
    byId("labReplayResourceNote").hidden = !replayMode;
    document.querySelector(".overview-layers").hidden = replayMode;
    layerBody.querySelector(".lab-kicker").hidden = replayMode;
    byId("labReplayLayersNote").hidden = !replayMode;
    byId("labSensitivityButton").hidden = replayMode;
    byId("labCommunityButton").innerHTML = `${icon("people")}${replayMode ? "Replay context" : "Community context"}`;
    byId("labCommunitiesTitle").textContent = replayMode ? "Replay context" : "Community context";
    byId("labCommunities").querySelector(".lab-dialog-head p").textContent = replayMode
      ? "Historical community impacts and the factors behind the replayed score."
      : "Annual population estimates and geographic context, separate from hazard exposure.";
    inspector.dataset.focused = String(!byId("placeCard").hidden);

    const selection = dataAdapter.selection;
    const snapshot = selection.data;
    byId("labEvidenceSummary").textContent = replayMode
      ? "The map is displaying a historical wildfire replay. Replay report times and citations appear in the assessment and briefing. The source drawer retains current-layer information separately."
      : selection.state === "stale"
        ? "This regional assessment has expired. It provides saved context and needs a refreshed source review before operational use."
        : selection.state === "live"
          ? "This saved assessment is within its stated validity period. Check its contributing sources before making a decision."
          : "A current regional assessment is unavailable. Missing data is shown explicitly; it does not indicate low risk.";
    byId("labEvidenceGenerated").textContent = replayMode ? "See the replay timeline" : snapshot?.generatedAt ? formatSnapshotTime(snapshot.generatedAt) : "Unavailable";
    byId("labEvidenceExpiry").textContent = replayMode ? "Historical record" : snapshot?.expiresAt ? formatSnapshotTime(snapshot.expiresAt) : "Unavailable";
    byId("labEvidenceSources").innerHTML = replayMode ? "CWFIS fire reports; see the replay briefing for citations." : snapshot?.sources?.length
      ? snapshot.sources.map((source) => {
        // Keep source links within the HTTP(S) schemes used by the pipeline.
        let url;
        try { url = new URL(source.url); } catch { return safe(source.name || "Source URL unavailable"); }
        return ["http:", "https:"].includes(url.protocol) ? `<a href="${safe(url.href)}" target="_blank" rel="noopener">${safe(source.name || url.hostname)}</a>` : safe(source.name || "Source URL unavailable");
      }).join("<br>") : "No source information available for this selection.";
    renderRegionalList();
  }

  // Watch data-driven regions only; updates to the new panels never retrigger this observer.
  const observer = new MutationObserver(scheduleSync);
  [byId("areaPanel"), byId("summary"), byId("placeCard"), byId("liveSummary")].forEach((target) => observer.observe(target, { childList: true, subtree: true, attributes: true }));
  observer.observe(document.body, { attributes: true, attributeFilter: ["class"] });
  observer.observe(byId("v-situation"), { attributes: true, attributeFilter: ["hidden"] });
  document.querySelectorAll(".hz button").forEach((button) => button.addEventListener("click", scheduleSync));
  byId("regionSelect").addEventListener("change", scheduleSync);
  byId("severity").addEventListener("input", scheduleSync);
  byId("sevReset").addEventListener("click", scheduleSync);

  // Secondary actions sit below the main decision surface.
  const actions = node(`<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
    <button class="btn ghost" type="button" id="labCommunityButton">${icon("people")}Community context</button>
    <button class="btn ghost" type="button" id="labSensitivityButton">${icon("scenario")}Sensitivity explorer</button>
  </div>`);
  document.querySelector(".map-col").append(actions);
  byId("labCommunityButton").onclick = () => byId("labCommunities").showModal();
  byId("labSensitivityButton").onclick = () => byId("labSensitivity").showModal();
  observer.observe(byId("tlTxt"), { childList: true, subtree: true });
  syncWorkspace();
  setTimeout(() => { map?.invalidateSize(); if (mode === "canada") map?.fitBounds(CANADA_BOUNDS); }, 100);
  window.addEventListener("resize", () => map?.invalidateSize());
})();
