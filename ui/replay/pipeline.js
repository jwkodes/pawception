/* Presentation only: all observations, merging and alerts come from Python. */
(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const video = el("pipeline-video");
  const fmt = value => `${Number(value).toFixed(2)}s`;
  let data, runId, selectedWindow, selectedAlert, busy = false, exportURL;

  function seek(time) {
    if (!data?.replay) return;
    video.currentTime = Math.max(data.replay.start_seconds, Math.min(data.replay.end_seconds, time));
    video.pause();
  }

  function bar(start, end, css, label, attributes = "") {
    const replay = data.replay;
    const length = replay.end_seconds - replay.start_seconds;
    const left = 100 * (start - replay.start_seconds) / length;
    const width = 100 * (end - start) / length;
    return `<button class="pipeline-bar ${css}" style="left:${left}%;width:${width}%" title="${esc(label)}" aria-label="${esc(label)}" ${attributes}></button>`;
  }

  function row(label, content) {
    return `<div class="pipeline-track-row"><span>${esc(label)}</span><div class="pipeline-track">${content}</div></div>`;
  }

  const eventName = id => id.replaceAll("_", " ").replace(/^./, c => c.toUpperCase());

  function drawTimeline() {
    if (!data?.replay) return;
    const replay = data.replay;
    let html = `<div class="pipeline-axis"><span>Source time</span><div>${[0,1,2,3,4].map(i => `<span>${fmt(replay.start_seconds+(replay.end_seconds-replay.start_seconds)*i/4)}</span>`).join("")}</div></div>`;
    const lanes = [];
    for (const window of replay.windows) {
      let lane = lanes.find(items => items.at(-1).end_seconds <= window.start_seconds + 0.000001);
      if (!lane) lanes.push(lane = []);
      lane.push(window);
    }
    for (const event of replay.task.events) {
      lanes.forEach((lane, index) => {
        const bars = lane.map(window => {
          const answer = window.detections.find(d => d.event_id === event.id);
          const verdict = answer?.status === "completed" ? answer.detected ? "Yes" : "No" : window.status;
          const css = answer?.status === "completed" ? answer.detected ? "observed" : "not_observed" : window.status;
          return bar(window.start_seconds,window.end_seconds,css,
            `${eventName(event.id)} · window ${window.id}: ${verdict}`,
            `data-window="${window.id}"`);
        }).join("");
        html += row(`${eventName(event.id)} · windows${lanes.length > 1 ? ` ${index + 1}` : ""}`,bars);
      });
      html += row("Detected intervals",replay.events.filter(e => e.event_id === event.id).map(e =>
        bar(e.start,e.end,"event",`${eventName(event.id)}: ${fmt(e.start)}–${fmt(e.end)}`,`data-seek="${e.start}"`)
      ).join(""));
    }
    html += row("Alerts",replay.alerts.map(alert =>
      bar(alert.at,alert.at,"alert",`${eventName(alert.event_id)}: ${alert.threshold_seconds}s continuous detection`, `data-alert="${alert.id}"`)
    ).join(""));
    html += row("Position",bar(video.currentTime,video.currentTime,"playhead","Video position") +
      bar(replay.summary.finalized_through,replay.summary.finalized_through,"frontier","Processed through here"));
    el("pipeline-timeline").innerHTML = html;
  }

  function drawInspector() {
    if (!data?.replay) return;
    const replay = data.replay;
    let html;
    if (selectedAlert) {
      const alert = replay.alerts.find(a => a.id === selectedAlert);
      if (!alert) { selectedAlert = null; return drawInspector(); }
      html = `
        <p class="eyebrow">ALERT EVIDENCE</p>
        <h3>${esc(eventName(alert.event_id))}</h3>
        <p>${esc(alert.reason)}</p>
        <p>Duration threshold reached: <strong>${fmt(alert.at)}</strong> on the source timeline<br>
          Confirmed by footage through ${fmt(alert.confirmed_through_seconds)}<br>
          Available after ${fmt(alert.available_after_run_seconds)} of processing</p>
        <p>Detected continuously for ${fmt(alert.detected_duration_seconds)} when confirmed.<br>
          Supporting windows: ${alert.supporting_window_ids.map(esc).join(", ")}.</p>
        <button class="text-button" data-seek="${alert.evidence_span.start}">Seek to evidence ↗</button>
        <details><summary>Supporting window decisions</summary>
          <pre>${esc(JSON.stringify(alert,null,2))}</pre></details>`;
    } else if (selectedWindow) {
      const window = replay.windows.find(w => w.id === selectedWindow);
      if (!window) { selectedWindow = null; return drawInspector(); }
      const record = window.record;
      const aligned = window.detections;
      html = `
        <p class="eyebrow">MODEL WINDOW ${window.id}</p>
        <h3>${fmt(window.start_seconds)}–${fmt(window.end_seconds)}</h3>
        <p>Status: <strong>${esc(window.status)}</strong><br>
          Input duration: ${fmt(window.input_duration_seconds)}<br>
          Upload: ${record.upload_seconds == null ? "Not measured" : fmt(record.upload_seconds)}<br>
          Inference: ${record.inference_seconds == null ? "Not measured" : fmt(record.inference_seconds)}<br>
          Total window work: ${record.processing_seconds == null ? "Not measured" : fmt(record.processing_seconds)}<br>
          Available after: ${record.finished_after_run_seconds == null ? "Pending" : fmt(record.finished_after_run_seconds)} of processing</p>
        <p>Absolute source time = local model time + <strong>${fmt(window.start_seconds)}</strong>.</p>
        <details open><summary>Model observations</summary>
          <pre>${esc(JSON.stringify(record.output || record.error || {status:window.status},null,2))}</pre></details>
        <details><summary>Event intervals on the source timeline</summary>
          <pre>${esc(JSON.stringify(aligned,null,2))}</pre></details>
        <details><summary>Exact prompt</summary><pre>${esc(record.prompt || "Not sent yet")}</pre></details>
        <details><summary>Usage, upload cleanup, and cache key</summary>
          <pre>${esc(JSON.stringify({usage:record.usage,upload_cleanup:record.upload_cleanup,cache_key:record.cache_key},null,2))}</pre></details>
        <button class="text-button" id="pipeline-raw">Load raw API response</button>
        <pre id="pipeline-raw-output"></pre>`;
    } else {
      const final = replay.snapshots.at(-1);
      html = `<p class="eyebrow">EVENT MONITORING</p>
        <h3>Through ${fmt(replay.summary.finalized_through)}</h3>
        <p>Alerts use continuous detected time. Overlapping intervals count once; separate bursts do not add up.</p>
        ${replay.task.events.map(event => `<h4>${esc(eventName(event.id))}</h4><p>${esc(event.description)}</p><p>Alert threshold: ${fmt(replay.alert_after_seconds[event.id])}<br>Longest detected stretch: ${fmt(final?.longest_detected_seconds[event.id] || 0)}</p>`).join("")}
        <p>Click a window to inspect its decision, or an alert to see the supporting detections.</p>`;
    }
    updateHTML(el("pipeline-inspector"), html);
  }

  function render() {
    const {run, replay} = data;
    el("pipeline-notice").classList.toggle("synthetic",run.provider === "mock");
    el("pipeline-notice").textContent = run.provider === "mock"
      ? "SYNTHETIC OBSERVATIONS — These responses test pipeline mechanics. They are not detections or ground-truth labels for this footage."
      : `${run.request.model} · Alerts based on continuous detected seconds · ${run.mode} recorded-video processing`;
    el("pipeline-config").textContent = JSON.stringify(run,null,2);
    if (!replay) { el("pipeline-summary").textContent = "Preparing video windows…"; return; }
    const stats = [[`${run.window_seconds}s / ${run.overlap_seconds}s`,"window / overlap"],[`${replay.summary.terminal_windows} / ${replay.summary.windows}`,"processed windows"],[fmt(replay.summary.finalized_through),"processed through"],[replay.alerts.length,"deduplicated alerts"]];
    el("pipeline-summary").innerHTML = stats.map(([value,label]) => `<div class="pipeline-stat"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`).join("");
    if (video.dataset.run !== run.id) {
      video.dataset.run = run.id;
      if (data.media_url) video.src = data.media_url;
      else video.removeAttribute("src");
      el("pipeline-scrubber").min = replay.start_seconds;
      el("pipeline-scrubber").max = replay.end_seconds;
      video.addEventListener("loadedmetadata",() => { video.currentTime = replay.start_seconds; },{once:true});
    }
    el("pipeline-alert-count").textContent = replay.alerts.length;
    el("pipeline-alerts").innerHTML = replay.alerts.map(a => `<button class="alert-card" data-alert="${a.id}"><span class="alert-time">${fmt(a.at)}<small>on source timeline</small></span><span><strong>${esc(eventName(a.event_id))}</strong><small>${esc(a.reason)} · processed after ${fmt(a.available_after_run_seconds)}</small></span><span class="arrow">↗</span></button>`).join("") || '<p class="empty-alerts">No detected interval has reached its duration threshold yet.</p>';
    if (exportURL) URL.revokeObjectURL(exportURL);
    exportURL = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));
    el("pipeline-export").href = exportURL;
    el("pipeline-export").download = `pawception-pipeline-${run.id}.json`;
    drawTimeline();
    drawInspector();
  }

  async function refresh() {
    if (busy || el("pipeline-view").hidden) return;
    busy = true;
    try {
      const indexResponse = await fetch("/api/pipeline");
      if (!indexResponse.ok) throw new Error("Pipeline endpoint unavailable; restart the local Python viewer.");
      const index = await indexResponse.json();
      if (!index.runs.some(r => r.id === runId)) runId = index.runs[0]?.id;
      el("pipeline-run").innerHTML = index.runs.map(r => `<option value="${r.id}" ${r.id===runId?"selected":""}>${esc(r.provider)} · ${esc(r.id)} · ${esc(r.status)}</option>`).join("");
      if (runId) {
        const response = await fetch(`/api/pipeline/${runId}`);
        if (!response.ok) throw new Error("Could not read the saved pipeline run");
        data = await response.json();
        render();
      } else el("pipeline-notice").textContent = "No pipeline runs yet. Run python -m scripts.run_pipeline from the terminal. Viewing this page makes no model calls.";
      el("pipeline-error").hidden = true;
      el("pipeline-sync").textContent = "Saved files · refreshes every 5s";
    } catch (error) {
      el("pipeline-error").textContent = error.message;
      el("pipeline-error").hidden = false;
    } finally { busy = false; }
  }

  el("pipeline-view").addEventListener("click",async event => {
    const target = event.target.closest("[data-window],[data-alert],[data-seek],#pipeline-raw");
    if (!target || !data?.replay) return;
    if (target.dataset.window) {
      selectedWindow = target.dataset.window; selectedAlert = null;
      seek(data.replay.windows.find(w => w.id === selectedWindow).start_seconds);
      drawInspector();
    } else if (target.dataset.alert) {
      selectedAlert = target.dataset.alert; selectedWindow = null;
      seek(data.replay.alerts.find(a => a.id === selectedAlert).evidence_span.start);
      drawInspector();
    } else if (target.dataset.seek) seek(Number(target.dataset.seek));
    else if (target.id === "pipeline-raw") {
      const requestedRun = runId;
      const requestedWindow = selectedWindow;
      const response = await fetch(`/api/pipeline/${requestedRun}/windows/${requestedWindow}`);
      const record = await response.json();
      if (runId === requestedRun && selectedWindow === requestedWindow && el("pipeline-raw-output")) {
        el("pipeline-raw-output").textContent = JSON.stringify(record,null,2);
      }
    }
  });
  video.addEventListener("timeupdate",() => {
    if (!data?.replay) return;
    if (video.currentTime < data.replay.start_seconds) video.currentTime = data.replay.start_seconds;
    if (video.currentTime > data.replay.end_seconds) { video.pause(); video.currentTime = data.replay.end_seconds; }
    el("pipeline-clock").textContent = fmt(video.currentTime);
    el("pipeline-scrubber").value = video.currentTime;
    drawTimeline();
  });
  el("pipeline-scrubber").addEventListener("input",event => seek(Number(event.target.value)));
  el("pipeline-restart").addEventListener("click",() => data?.replay && seek(data.replay.start_seconds));
  el("pipeline-run").addEventListener("change",event => { runId=event.target.value; selectedWindow=null; selectedAlert=null; refresh(); });
  el("pipeline-refresh").addEventListener("click",refresh);
  window.refreshPipeline = refresh;
  setInterval(refresh,5000);
  refresh();
})();
