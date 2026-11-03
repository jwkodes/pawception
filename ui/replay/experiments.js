/* Read-only developer view. All requests go to the local artifact reader. */
"use strict";
let experimentRunId = null, experimentClipId = null, experimentData = null;
let experimentBusy = false, experimentView = "experiments", experimentBlob;
const money = value => value == null ? "Not available" : `$${value.toFixed(6)}`;
const numeric = value => value == null ? "Not recorded" : Number(value).toLocaleString();
const duration = value => value == null ? "Not recorded" : `${value.toFixed(1)}s`;
const localDate = value => value ? new Date(value).toLocaleString(undefined,{timeZone:"Asia/Singapore",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"}) : "Unknown start";
const cell = (name,value) => `<div class="config-cell"><dt>${esc(name)}</dt><dd>${esc(value ?? "Not recorded")}</dd></div>`;
const badge = value => `<span class="experiment-badge ${esc(value)}">${esc((value || "not recorded").replaceAll("_"," "))}</span>`;
const safeLink = url => typeof url === "string" && /^https:\/\//.test(url) ? esc(url) : "#";
const jsonText = value => esc(JSON.stringify(value,null,2));
const modeLabels = {describe:"Visible observations",hazards:"Hazard assessment",monitor:"Monitoring observations"};
const evalPercent = value => value == null ? "Not measured" : `${(100 * value).toFixed(1)}%`;
const evalIntervals = intervals => Array.isArray(intervals) ? intervals.map(i => `${i.start}–${i.end}s`).join(", ") || "No events" : "Unlabeled";
function evaluationOutput(evaluation) {
  if (!evaluation) return "";
  if (evaluation.error) return `<section class="experiment-card"><h2>Automated evaluation</h2><p>${esc(evaluation.error)}</p></section>`;
  const m = evaluation.summary, timing = m.timing;
  const rows = evaluation.cases.filter(c => c.scored).map(c => {
    const verdict = !c.correct ? c.prediction === "unknown" ? "abstained" : "miss" : c.timing?.passed === false || c.interval_errors.length ? "timing_mismatch" : "pass";
    return `<tr><td>${esc(c.clip_id)}<small>${esc(c.variant)} · ${esc(c.behavior)}</small></td><td>${esc(c.expected)}<small>${esc(evalIntervals(c.expected_intervals))}</small></td><td>${esc(c.prediction)}<small>${esc(evalIntervals(c.predicted_intervals))}</small></td><td>${esc(evalPercent(c.timing?.iou))}</td><td>${badge(verdict)}</td></tr>`;
  }).join("");
  return `<section class="experiment-card" aria-label="Automated evaluation"><div class="experiment-card-top"><div><h2>Automated evaluation</h2><p>Annotation set v${esc(evaluation.dataset_version)} · ${m.scored_cases} scored cases · ${m.independent_clips} independent clips</p></div>${badge(evaluation.gate.status)}</div><div class="experiment-stats"><div class="experiment-stat"><strong>${esc(evalPercent(m.accuracy))}</strong><span>behavior accuracy, including abstentions</span></div><div class="experiment-stat"><strong>${esc(evalPercent(m.positive_recall))}</strong><span>positive event recall</span></div><div class="experiment-stat"><strong>${esc(evalPercent(timing.mean_iou))}</strong><span>mean temporal overlap</span></div><div class="experiment-stat"><strong>${m.abstentions}</strong><span>abstentions · ${m.invalid_interval_cases} invalid interval cases</span></div></div><p class="experiment-note">Scored locally against saved labels. Ground truth is never sent to Muse. Proposed/unlabeled cases are excluded; ${m.negative_cases ? `${m.negative_cases} confirmed negative cases scored.` : "no confirmed negative cases are available in this run, so false-alarm performance is not established."}</p>${rows ? `<div class="evaluation-table-wrap"><table class="evaluation-table"><thead><tr><th>Clip / behavior</th><th>Expected</th><th>Predicted</th><th>Time overlap</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p>No confirmed, assessable event labels were scored.</p>'}${evaluation.gate.reasons.length ? `<ul class="evaluation-reasons">${evaluation.gate.reasons.map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>` : ""}<details class="experiment-disclosure" data-detail="evaluation-json"><summary>Labels, metrics, skipped cases, and full evaluation JSON</summary><pre>${jsonText(evaluation)}</pre></details></section>`;
}
function monitoringOutput(record) {
  if (record?.structured_output && !record.structured_output.assessments) {
    return `<p class="experiment-note">Schema and interval validation: ${esc(record.validation_status)}. Simple format; coverage is not measured and these observations have not been passed to the temporal engine.</p><div class="monitor-assessments">${Object.entries(record.structured_output).map(([rule, assessment]) => `<section class="monitor-assessment"><div class="experiment-card-top"><h3>${esc(rule)}</h3>${badge(assessment.assessment)}</div><p>${esc(assessment.evidence)}</p>${assessment.intervals.map(interval => `<div class="monitor-interval"><strong>${esc(interval.start)}–${esc(interval.end)}s</strong></div>`).join("")}${assessment.uncertainty ? `<p>Uncertainty: ${esc(assessment.uncertainty)}</p>` : ""}</section>`).join("")}</div><details class="experiment-disclosure" data-detail="monitor-json"><summary>Exact structured observation JSON</summary><pre>${jsonText(record.structured_output)}</pre></details>`;
  }
  if (!record?.structured_output?.assessments) return "";
  return `<p class="experiment-note">Schema and semantic validation: ${esc(record.validation_status)}. These observations have not been passed to the temporal engine; no notification was generated.</p><div class="monitor-assessments">${record.structured_output.assessments.map(assessment => `<section class="monitor-assessment"><div class="experiment-card-top"><h3>${esc(assessment.rule_id)}</h3>${badge(assessment.outcome)}</div><p>Visibility: ${esc(assessment.visibility)}</p><p>${esc(assessment.explanation)}</p>${assessment.events.map(event => `<div class="monitor-interval"><strong>${esc(event.start_seconds)}–${esc(event.end_seconds)}s</strong><p>${esc(event.evidence)}</p>${event.uncertainties.length ? `<p>Uncertainties: ${event.uncertainties.map(esc).join("; ")}</p>` : ""}</div>`).join("")}${assessment.coverage_gaps.map(gap => `<div class="monitor-interval coverage-unknown"><strong>Unknown coverage · ${esc(gap.start_seconds)}–${esc(gap.end_seconds)}s</strong><p>${esc(gap.reason)}</p></div>`).join("")}</section>`).join("")}</div><details class="experiment-disclosure" data-detail="monitor-json"><summary>Exact structured observation JSON</summary><pre>${jsonText(record.structured_output)}</pre></details>`;
}
function updateHTML(element, html) {
  if (element._rendered === html) return;
  const open = [...element.querySelectorAll("details[open][data-detail]")].map(d => d.dataset.detail);
  element.innerHTML = html; element._rendered = html;
  for (const detail of element.querySelectorAll("details[data-detail]")) detail.open = open.includes(detail.dataset.detail);
}
function setDeveloperView(view) {
  experimentView = ["mock", "pipeline"].includes(view) ? view : "experiments";
  for (const name of ["experiments", "mock", "pipeline"]) {
    $(`${name}-view`).hidden = experimentView !== name;
    $(`${name}-tab`).setAttribute("aria-selected", experimentView === name);
  }
  const labels = {experiments:"Saved Muse results", mock:"Synthetic observations", pipeline:"Recorded video pipeline"};
  $("data-badge").innerHTML = `<span></span>${labels[experimentView]}`;
  $("data-badge").dataset.shortLabel = experimentView === "experiments" ? "Muse" : experimentView;
  history.replaceState(null,"",`#${experimentView}`);
  if (experimentView === "experiments") { pause(); refreshExperiments(); }
  else document.querySelector("#experiment-video video")?.pause();
  if (experimentView !== "pipeline") $("pipeline-video").pause();
  if (experimentView === "pipeline") { pause(); window.refreshPipeline?.(); }
}
function renderRunList(runs) {
  $("run-total").textContent = runs.length;
  updateHTML($("experiment-runs"),runs.map(run => `<button class="run-choice" data-run="${esc(run.id)}" aria-pressed="${run.id === experimentRunId}"><strong>${esc(localDate(run.started_at))} SGT</strong><small>${esc(run.model)}<br>${run.summary.clips} clips · ${run.summary.completed} completed analyses</small><span class="run-choice-status"><span>${esc(run.status.replaceAll("_"," "))}</span><span>${run.summary.estimated_cost_usd == null ? "—" : money(run.summary.estimated_cost_usd)}</span></span></button>`).join("") || '<p class="experiment-empty">No saved runs yet.</p>');
}
function renderExperiment(run) {
  const detail = $("experiment-detail");
  if (detail.dataset.run !== run.id) {
    detail.dataset.run = run.id;
    detail.innerHTML = '<div id="experiment-summary"></div><div id="experiment-config"></div><div id="experiment-clip-select" class="clip-choices" aria-label="Select experiment clip"></div><section class="experiment-card" id="experiment-clip-card"><div class="clip-context"><div id="experiment-video"></div><div id="experiment-clip-context"></div></div><div id="experiment-clip-footer" class="clip-footer"></div></section><div id="experiment-outputs" class="analysis-grid"></div><a id="export-experiment" class="experiment-export">Export this run JSON ↗</a>';
  }
  const s = run.summary;
  if (!$("experiment-evaluation")) {
    const evaluation = document.createElement("div"); evaluation.id = "experiment-evaluation";
    $("experiment-summary").after(evaluation);
  }
  updateHTML($("experiment-evaluation"),evaluationOutput(run.evaluation));
  $("experiment-outputs").classList.toggle("single-analysis",run.modes?.length === 1);
  updateHTML($("experiment-summary"),`<section class="experiment-card"><div class="experiment-card-top"><div><h2>${esc(run.model)}</h2><p class="run-id">${esc(run.id)}</p></div>${badge(run.status)}</div><div class="experiment-stats"><div class="experiment-stat"><strong>${s.completed}${s.planned_analyses ? ` / ${s.planned_analyses}` : ""}</strong><span>completed analyses · ${s.clips} clips saved</span></div><div class="experiment-stat"><strong>${s.estimated_cost_usd == null ? "—" : money(s.estimated_cost_usd)}</strong><span>estimated token cost · ${s.usage_records}/${s.analyses} usage records</span></div><div class="experiment-stat"><strong>${run.max_output_tokens?.toLocaleString() ?? "—"}</strong><span>output token cap per call, including reasoning</span></div><div class="experiment-stat"><strong>${run.finished_at ? "Finished" : "Last saved"}</strong><span>${esc(localDate(run.finished_at || run.started_at))} SGT</span></div></div><p class="experiment-note">Saved status reflects the experiment files; it is not a process heartbeat. Costs use reported tokens and exclude any unreported usage or other charges.</p></section>`);
  const rates = run.pricing.per_million_tokens[run.model];
  updateHTML($("experiment-config"),`<section class="experiment-card"><h2>What was sent</h2><dl class="config-grid">${cell("Requested model",run.model)}${cell("Endpoint",run.endpoint)}${cell("Analysis modes",run.modes?.join(" + "))}${cell("Response format",run.response_format ? `${run.response_format.type} · ${run.response_format.strict == null ? "strict omitted (API default)" : `strict ${run.response_format.strict}`}` : "Text (API default)")}${cell("HTTP timeout",run.timeout_seconds == null ? null : `${run.timeout_seconds}s`)}${cell("Automatic retries",run.max_retries)}${cell("Response storage",run.store_responses == null ? null : String(run.store_responses))}${cell("Reasoning effort",run.reasoning_effort)}${run.configuration_provenance?.request_options ? cell("Streaming",String(run.configuration_provenance.request_options.stream ?? false)) + cell("Temperature",run.configuration_provenance.request_options.temperature) + cell("Top-p",run.configuration_provenance.request_options.top_p) : ""}${cell("Video sampling",run.video_sampling)}${cell("Upload retention",run.keep_uploads == null ? null : run.keep_uploads ? "Retain" : "Delete after clip")}</dl><p class="experiment-note">${run.model?.endsWith("-contributor") ? "Contributor mode: Meta may use submitted content and responses for model training. " : "Standard mode. "}${rates ? `Prices per 1M tokens: input $${rates.input}, cached input $${rates.cached_input}, output $${rates.output}. ` : "Price for this model is not configured. "}<a href="${safeLink(run.pricing.source)}" target="_blank" rel="noreferrer">Meta pricing · ${esc(run.pricing.as_of)}</a></p><details class="experiment-disclosure" data-detail="environment"><summary>Environment, hashes, and full request configuration</summary><dl class="config-grid">${cell("Python",run.python_version)}${cell("Pinned Python",run.python_version_pin)}${cell("SDK",run.sdk_version ? `openai ${run.sdk_version}` : null)}${cell("Started (UTC)",run.started_at)}${cell("Finished (UTC)",run.finished_at)}${cell("uv.lock SHA-256",run.uv_lock_sha256)}</dl><pre>${jsonText(Object.fromEntries(Object.entries(run).filter(([key]) => !["clips","pricing","summary","evaluation"].includes(key))))}</pre><p>“Not recorded” means an older artifact omitted that setting. Current runner defaults: retries 0, response storage false, API-managed reasoning and video sampling. Viewing this page never reads or displays .env.</p></details></section>`);
  if (!run.clips.some(clip => clip.id === experimentClipId)) experimentClipId = run.clips[0]?.id;
  updateHTML($("experiment-clip-select"),run.clips.map(clip => `<button class="clip-choice" data-clip="${esc(clip.id)}" aria-pressed="${clip.id === experimentClipId}">${esc(clip.filename)}${clip.evaluation_trial ? ` · ${esc(clip.evaluation_trial.variant)} · trial ${esc(clip.evaluation_trial.repeat)}` : ""}</button>`).join(""));
  const clip = run.clips.find(clip => clip.id === experimentClipId);
  $("experiment-clip-card").hidden = !clip;
  if (!clip) {
    updateHTML($("experiment-outputs"),'<p class="experiment-empty">No clip artifacts yet. New runs appear here as upload and inference progress is saved.</p>');
  } else {
    const video = $("experiment-video");
    // Keep the media element mounted during polling, so playback isn't interrupted.
    if (video.dataset.clip !== `${run.id}/${clip.id}`) {
      video.dataset.clip = `${run.id}/${clip.id}`;
      video.innerHTML = clip.media_url ? `<video class="clip-video" controls preload="metadata" src="${esc(clip.media_url)}" ${clip.poster_url ? `poster="${esc(clip.poster_url)}"` : ""} aria-label="Experiment video ${esc(clip.filename)}"></video>` : '<div class="analysis-placeholder">Local video unavailable. Restore it with the test-video preparation script.</div>';
    }
    const meta = clip.metadata;
    updateHTML($("experiment-clip-context"),`<h3 class="clip-source-title">${esc(clip.filename)}</h3>${badge(clip.status || (run.finished_at ? "saved" : "in_progress"))}<dl class="config-grid">${cell("Source duration",duration(meta.duration_seconds))}${cell("Upload",`${(clip.bytes/1_000_000).toFixed(1)} MB · ${duration(clip.upload_seconds)}`)}${cell("Source resolution",meta.width ? `${meta.width} × ${meta.height} · ${meta.fps.toFixed(2)} fps` : null)}${cell("Uploaded filename",clip.upload_filename)}${cell("Upload cleanup",clip.upload_cleanup || "Not finalized")}${cell("File ID",clip.file_id)}</dl><p>Local filenames and source labels help us inspect clips. The model receives the neutral name <code>${esc(clip.upload_filename || "clip.mp4")}</code> and the prompt shown below.</p>`);
    updateHTML($("experiment-clip-footer"),`${clip.source ? `<a href="${safeLink(clip.source.source_url)}" target="_blank" rel="noreferrer">${esc(clip.source.creator_credit)} ↗</a><a href="${safeLink(clip.source.license_url)}" target="_blank" rel="noreferrer">${esc(clip.source.license)}</a><br>${esc(clip.source.annotations_status)}<br>` : ""}SHA-256: ${esc(clip.sha256)}${clip.upload_error ? `<br>Upload error: ${jsonText(clip.upload_error)}` : ""}${clip.cleanup_error ? `<br>Cleanup error: ${jsonText(clip.cleanup_error)}` : ""}`);
    updateHTML($("experiment-outputs"),(run.modes || ["describe","hazards"]).map(mode => {
      const record = clip.analyses[mode];
      const usage = record?.usage || {};
      const reasoning = usage.output_tokens_details?.reasoning_tokens;
      const cached = usage.input_tokens_details?.cached_tokens;
      return `<section class="experiment-card analysis-card" aria-label="${esc(modeLabels[mode] || mode)} result"><div class="experiment-card-top"><h2>${esc(modeLabels[mode] || mode)}</h2>${badge(record?.status || (run.finished_at ? "not_recorded" : "not_yet_saved"))}</div><div class="analysis-metrics"><span><strong>${duration(record?.inference_seconds)}</strong> inference</span><span><strong>${numeric(usage.input_tokens)}</strong> input tokens</span><span><strong>${numeric(usage.output_tokens)}</strong> output tokens</span><span><strong>${money(record?.estimated_cost_usd)}</strong> estimated</span></div><details class="experiment-disclosure" data-detail="${mode}-prompt"><summary>Exact prompt sent to Muse</summary><pre>${esc(record?.prompt || run.prompts?.[mode] || "Prompt not recorded.")}</pre><p>Prompt SHA-256: ${esc(record?.prompt_sha256 || run.prompt_sha256?.[mode] || "Not recorded")}</p></details>${record?.validation_error ? `<p class="validation-error">Validation failed: ${esc(record.validation_error)}</p>` : ""}${mode === "monitor" && record?.structured_output ? monitoringOutput(record) : record?.output_text ? `<pre class="model-output">${esc(record.output_text)}</pre>` : `<div class="analysis-placeholder">${record?.error ? `Call failed: ${jsonText(record.error)}` : record?.status === "incomplete" ? "The model response is incomplete; inspect the raw response below." : "No response text saved yet."}</div>`}<details class="experiment-disclosure" data-detail="${mode}-response"><summary>Tokens, IDs, and raw response JSON</summary><dl class="config-grid">${cell("Returned model",record?.returned_model)}${cell("Response ID",record?.response_id)}${cell("Response status",record?.response_status)}${cell("Cached input tokens",numeric(cached))}${cell("Reasoning tokens",numeric(reasoning))}${cell("Total tokens",numeric(usage.total_tokens))}</dl><p>Reasoning tokens are included in output tokens. They are not charged twice. An incomplete response is not a successful analysis.</p><pre>${jsonText(record?.response || record || {status:"No analysis record yet"})}</pre></details></section>`;
    }).join(""));
  }
  if (experimentBlob) URL.revokeObjectURL(experimentBlob);
  experimentBlob = URL.createObjectURL(new Blob([JSON.stringify(run,null,2)],{type:"application/json"}));
  $("export-experiment").href = experimentBlob;
  $("export-experiment").download = `pawception-muse-${run.id}.json`;
}
async function refreshExperiments() {
  if (experimentBusy || experimentView !== "experiments") return;
  experimentBusy = true; $("refresh-experiments").disabled = true;
  try {
    const indexResponse = await fetch("/api/experiments");
    if (!indexResponse.ok) throw new Error(`Local artifact endpoint returned ${indexResponse.status}`);
    const index = await indexResponse.json();
    if (!index.runs.some(run => run.id === experimentRunId)) experimentRunId = index.runs[0]?.id;
    renderRunList(index.runs);
    if (experimentRunId) {
      const response = await fetch(`/api/experiments/${encodeURIComponent(experimentRunId)}`);
      if (!response.ok) throw new Error(`Could not read run (${response.status})`);
      experimentData = await response.json();
      renderExperiment(experimentData);
    } else $("experiment-detail").innerHTML = '<div class="experiment-empty">No saved Muse runs yet. Run the experiment script from your terminal; this page will pick up its results. No API calls are triggered here.</div>';
    $("experiments-error").hidden = true;
    $("experiment-sync").textContent = `Checked ${new Date().toLocaleTimeString(undefined,{timeZone:"Asia/Singapore"})} SGT · local files`;
  } catch (error) {
    $("experiments-error").textContent = `${error.message}. If you just updated this UI, restart the Python server (Ctrl-C, then uv run --locked python -m scripts.replay_ui) and refresh. Previous results remain visible.`;
    $("experiments-error").hidden = false;
    $("experiment-sync").textContent = "Results could not be refreshed";
  } finally { experimentBusy = false; $("refresh-experiments").disabled = false; }
}
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click",() => setDeveloperView(button.dataset.view)));
$("refresh-experiments").addEventListener("click",refreshExperiments);
$("experiment-runs").addEventListener("click",event => {
  const button = event.target.closest("[data-run]");
  if (button && !experimentBusy) { experimentRunId = button.dataset.run; experimentClipId = null; refreshExperiments(); }
});
$("experiment-detail").addEventListener("click",event => {
  const button = event.target.closest("button[data-clip]");
  if (button) { experimentClipId = button.dataset.clip; renderExperiment(experimentData); }
});
setInterval(() => { if ($("auto-refresh").checked && !document.hidden) refreshExperiments(); },5000);
setDeveloperView(location.hash.slice(1));
