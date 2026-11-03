/* The Python engine supplies every evaluation. This file only presents its replay. */
"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clock = value => `${String(Math.floor(value / 60)).padStart(2,"0")}:${String(Math.floor(value % 60)).padStart(2,"0")}`;
const seconds = value => `${Number(value.toFixed(1))}s`;
let replay, position = 30, playing = false, selected = null, lastFrame = null, exportURL;
const finalSnapshot = () => replay.snapshots.at(-1);
const snapshot = () => replay.snapshots.filter(s => s.time <= position).at(-1) || replay.snapshots[0];
const ruleName = id => replay.rules.find(r => r.id === id)?.kind === "repeated" ? "Sofa chewing" : "Water-bowl visit";
const evaluation = (snap, id) => snap.evaluations.find(e => e.rule_id === id);
const pct = time => 100 * time / replay.duration;
const availableTime = time => replay.snapshots.find(s => s.time >= time)?.time ?? replay.duration;
const isFuture = known => known > snapshot().time;
const futureClass = known => isFuture(known) ? ($("show-future").checked ? " future" : " hidden-future") : "";
const span = (label, value) => `<div class="stat-pair"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
const refs = values => values?.length ? `<ul class="evidence-list">${values.map(v => `<li>${esc(v)}</li>`).join("")}</ul>` : "<p class=\"inspector-copy\">No positive event evidence. This decision uses observation coverage.</p>";

function bar(start, end, css, label, kind, id, known, extra = "") {
  const active = selected?.kind === kind && selected.id === id ? " selected" : "";
  return `<button class="bar ${css}${futureClass(known)}${active}" style="left:${pct(start)}%;width:${Math.max(.5,pct(end-start))}%" data-kind="${kind}" data-id="${esc(id)}" data-known="${known}" title="${esc(label)}" aria-label="${esc(label)}">${extra}</button>`;
}
function row(label, subtitle, content, group = false) {
  return `<div class="timeline-row${group ? " group-start" : ""}"><div class="row-label">${label}<span>${subtitle}</span></div><div class="track">${content}</div></div>`;
}
function drawTimeline(snap) {
  let html = `<div class="time-axis"><span></span><div class="axis-track">${Array.from({length:9},(_,i) => `<span class="axis-label" style="left:${i*12.5}%">${clock(replay.duration*i/8)}</span>`).join("")}</div></div>`;
  for (const rule of replay.rules) {
    const observations = replay.observations.filter(o => o.rule_id === rule.id);
    const tracks = [];
    // Preserve overlapping model windows on separate rows.
    for (const o of [...observations].sort((a,b) => a.start-b.start)) {
      let lane = tracks.find(t => t.at(-1).end <= o.start);
      if (!lane) tracks.push(lane = []);
      lane.push(o);
    }
    tracks.forEach((lane,index) => {
      html += row(index ? "Overlapping report" : ruleName(rule.id), index ? "raw observation" : "raw observations", lane.map(o => {
        const known = availableTime(o.end);
        const label = `${o.id}: ${seconds(o.start)}–${seconds(o.end)}, ${o.outcome.replaceAll("_"," ")}; replay available at ${seconds(known)}`;
        return bar(o.start,o.end,o.outcome === "observed" ? "observed" : o.outcome === "unknown" ? "gap" : "quiet",label,"observation",o.id,known) + (o.events || []).map(event => bar(event.start,event.end,"event",`Event in ${o.id}: ${seconds(event.start)}–${seconds(event.end)}`,"observation",o.id,known)).join("");
      }).join(""), index === 0);
    });
    const ev = evaluation(snap,rule.id), final = evaluation(finalSnapshot(),rule.id);
    let coverage = final.coverage.map((c,index) => bar(c.start,c.end,"coverage future",`Future coverage ${seconds(c.start)}–${seconds(c.end)}`,"coverage",rule.id,replay.duration)).join("");
    coverage += ev.coverage.map(c => bar(c.start,c.end,"coverage",`Confirmed coverage ${seconds(c.start)}–${seconds(c.end)}`,"coverage",rule.id,0)).join("");
    coverage += observations.filter(o => o.outcome === "unknown").map(o => bar(o.start,o.end,"gap",`Coverage gap ${seconds(o.start)}–${seconds(o.end)}`,"observation",o.id,o.end)).join("");
    html += row("Coverage","reliable visibility",coverage);
    const knownIds = new Set(ev.episodes.map(e => e.id));
    const episodes = [...ev.episodes.map(e => ({...e,known:0})),...final.episodes.filter(e => !knownIds.has(e.id)).map(e => ({...e,known:replay.snapshots.find(s => evaluation(s,rule.id).episodes.some(episode => episode.id === e.id)).time}))];
    html += row("Merged episodes",`${ev.episodes.length} known · ${rule.kind === "repeated" ? `${ev.count} in window` : "bowl visits"}`,episodes.map((e,index) => bar(e.start,e.end,"episode",`Episode ${index+1}: ${seconds(e.start)}–${seconds(e.end)}, ${e.evidence.length} evidence reference(s)`,"episode",`${rule.id}|${e.id}`,e.known)).join(""));
    html += row("Alerts","onset → available",finalSnapshot().alerts.filter(a => a.rule_id === rule.id).map(a => {
      const future = futureClass(a.detected_at);
      const active = selected?.kind === "alert" && selected.id === a.incident_id ? " selected" : "";
      const attributes = `data-kind="alert" data-id="${a.incident_id}" data-known="${a.detected_at}"`;
      return `<span class="alert-connector${future}" style="left:${pct(a.at)}%;width:${pct(a.detected_at-a.at)}%"></span><button class="alert-node${future}${active}" style="left:${pct(a.at)}%" ${attributes} title="Condition true at ${seconds(a.at)}" aria-label="${esc(ruleName(a.rule_id))}: condition true at ${seconds(a.at)}"></button><button class="alert-node detected${future}${active}" style="left:${pct(a.detected_at)}%" ${attributes} title="Result available at ${seconds(a.detected_at)}" aria-label="${esc(ruleName(a.rule_id))}: alert available at ${seconds(a.detected_at)}"></button>`;
    }).join(""));
  }
  html += `<div class="playhead" style="left:calc(124px + (100% - 124px) * ${position/replay.duration})"><span></span></div>`;
  $("timeline").innerHTML = html;
}

function drawInspector(snap) {
  let html = "";
  if (!selected) {
    html = `<p class="inspector-eyebrow">ENGINE STATE</p><h3 class="inspector-title">Through ${clock(snap.time)}</h3><p class="inspector-copy">Only completed analysis is used below. Scrubbing between results holds the last known state.</p>`;
    for (const rule of replay.rules) {
      const ev = evaluation(snap,rule.id);
      html += `<h4>${ruleName(rule.id)} <span class="state-label ${ev.state}">${ev.state}</span></h4>`;
      html += rule.kind === "repeated" ? span("Episodes in window",`${ev.count} / ${rule.threshold}`) + span("Window",`${seconds(ev.window_start)}–${seconds(snap.time)}`) : span("Last visit ended",ev.last_seen == null ? "None observed" : seconds(ev.last_seen)) + span("Absence timer from",ev.absence_since == null ? "Awaiting coverage" : seconds(ev.absence_since)) + span("Next due",ev.due_at == null ? "Unknown" : seconds(ev.due_at));
    }
    const missing = snap.evaluations.find(e => e.kind === "missing");
    const note = missing.last_seen != null ? `The visit ending at ${seconds(missing.last_seen)} resets the missing-event timer. A confirmed visit can resolve an open absence incident.` : snap.time >= 20 && replay.settings.include_gap ? "The 15–20s blind interval breaks the missing-event timer. Coverage after the gap must establish a new absence interval; recovery alone does not clear an existing incident." : "Click the first merged chewing episode to see how overlapping observations become one event.";
    html += `<div class="inspector-note">${note}</div>`;
  } else if (selected.kind === "observation") {
    const o = replay.observations.find(o => o.id === selected.id);
    html = `<p class="inspector-eyebrow">${o.outcome === "unknown" ? "COVERAGE GAP" : "RAW OBSERVATION"}</p><h3 class="inspector-title">${esc(o.id)}</h3>${span("Rule",ruleName(o.rule_id))}${span("Source window",`${seconds(o.start)}–${seconds(o.end)}`)}${span("Outcome",o.outcome.replaceAll("_"," "))}${span("Replay available",seconds(availableTime(o.end)))}<p class="inspector-copy">${o.outcome === "unknown" ? "Visibility is unknown. The engine cannot claim that a bowl visit did not happen in this interval. Continuous absence coverage must restart after the gap." : o.outcome === "observed" ? "The model reports the behavior inside this window. The engine merges overlapping event intervals before counting repetitions." : "The window was observable for this rule and pet, and no qualifying event was reported."}</p>${(o.events || []).map(e => span("Event",`${seconds(e.start)}–${seconds(e.end)}`)).join("")}${refs((o.events || []).map(e => e.evidence))}`;
  } else if (selected.kind === "episode") {
    const [ruleId,id] = selected.id.split("|");
    const ev = evaluation(snap,ruleId), e = ev.episodes.find(e => e.id === id);
    if (e) {
      const supporting = replay.observations.filter(o => snap.available_observations.includes(o.id) && (o.events || []).some(event => e.evidence.includes(event.evidence)));
      html = `<p class="inspector-eyebrow">MERGED EPISODE</p><h3 class="inspector-title">One episode.<br>${supporting.length} report${supporting.length === 1 ? "" : "s"}.</h3>${span("Rule",ruleName(ruleId))}${span("Event interval",`${seconds(e.start)}–${seconds(e.end)}`)}${span("Evidence references",e.evidence.length)}<p class="inspector-copy">${supporting.length > 1 ? "These event intervals overlap or meet the merge rule. Multiple reports support the same episode, so they do not inflate the count." : "A distinct episode supported by a completed model observation."}</p><h4>Supporting observations</h4>${supporting.map(o => span(o.id,`${seconds(o.start)}–${seconds(o.end)}`)).join("")}${refs(e.evidence)}`;
    } else selected = null;
  } else if (selected.kind === "alert") {
    const a = snap.alerts.find(a => a.incident_id === selected.id);
    if (a) {
      const incident = snap.incidents.find(i => i.id === a.incident_id);
      html = `<p class="inspector-eyebrow">ALERT EVIDENCE</p><h3 class="inspector-title">${ruleName(a.rule_id)}</h3>${span("Condition true",seconds(a.at))}${span("Analysis through",seconds(a.detected_at))}${span("Replay delay",seconds(a.detected_at-a.at))}${span("Incident",incident.status)}${incident.resolved_at == null ? "" : span("Resolved at",seconds(incident.resolved_at))}<p class="inspector-copy">${esc(a.reason)}.</p><div class="inspector-note">${a.due_at != null ? "A missing-event alert is supported by reliable coverage, rather than a video of something happening." : "The repetition threshold counts distinct episodes after merging overlapping reports."} Replay delay reflects this fixture’s analysis checkpoints, not measured model latency.</div>${a.due_at == null ? "" : span("Timer due",seconds(a.due_at))}<h4>Supporting observation IDs</h4>${refs(a.observation_ids)}<h4>Event evidence</h4>${refs(a.evidence)}`;
      if (a.due_at != null) {
        const rule = replay.rules.find(r => r.id === a.rule_id);
        html += `<h4>Absence evidence</h4>${span("Covered absence",`${seconds(a.at-rule.window_seconds)}–${seconds(a.at)}`)}${span("Last visit ended",a.last_seen == null ? "None observed" : seconds(a.last_seen))}`;
      }
    } else selected = null;
  } else {
    const ev = evaluation(snap,selected.id);
    html = `<p class="inspector-eyebrow">OBSERVATION COVERAGE</p><h3 class="inspector-title">${ruleName(selected.id)}</h3><p class="inspector-copy">Reliable coverage is the union of observable windows, with unknown intervals removed. It belongs to this rule and pet; a visible room alone does not prove that the pet was observable.</p>${ev.coverage.map(c => span("Covered interval",`${seconds(c.start)}–${seconds(c.end)}`)).join("") || "<p class=\"inspector-copy\">No completed coverage yet.</p>"}<div class="inspector-note">For missing events, a blind interval cannot count toward the absence timer. For repeated events, a blind interval cannot establish that one episode stopped and another began.</div>`;
  }
  if (!html) return drawInspector(snap);
  $("inspector").innerHTML = `<div class="inspector-content">${selected ? '<button class="text-button back-button" id="back-overview">← Engine overview</button>' : ""}${html}</div>`;
  $("back-overview")?.addEventListener("click",() => { selected = null; render(); });
}

function render() {
  if (!replay) return;
  const snap = snapshot();
  $("clock").textContent = clock(position);
  $("duration").textContent = clock(replay.duration);
  $("scrubber").value = position;
  $("analysis-clock").textContent = `Analysis complete through ${clock(snap.time)}`;
  $("inspector-status").textContent = `At ${clock(snap.time)}`;
  $("next-result").disabled = !replay.snapshots.some(s => s.time > position);
  $("next-alert").disabled = !finalSnapshot().alerts.some(a => a.detected_at > position);
  drawTimeline(snap);
  drawInspector(snap);
  $("alert-count").textContent = snap.alerts.length;
  $("alert-list").innerHTML = snap.alerts.map(a => {
    const incident = snap.incidents.find(i => i.id === a.incident_id);
    return `<button class="alert-card" data-kind="alert" data-id="${a.incident_id}" data-known="${a.detected_at}"><span class="alert-time">${clock(a.at)}<small>available ${clock(a.detected_at)}</small></span><span><strong>${ruleName(a.rule_id)}</strong><small>${esc(a.reason)} · ${incident.status}</small></span><span class="arrow">↗</span></button>`;
  }).join("") || '<p class="empty-alerts">No alerts available yet. Advance to the next analysis result.</p>';
}
function pause() {
  playing = false; lastFrame = null;
  $("play-icon").textContent = "▶";
  $("play").setAttribute("aria-label","Play replay");
}
function seek(time, keepSelection = false) {
  pause(); position = Math.max(0,Math.min(replay.duration,time));
  if (!keepSelection) selected = null;
  render();
}
function animate(stamp) {
  if (!playing) return;
  if (lastFrame !== null) {
    const previous = position;
    position = Math.min(replay.duration,position + Math.min((stamp-lastFrame)/1000,.25) * Number($("speed").value));
    if (Math.floor(position * 10) !== Math.floor(previous * 10)) render();
  }
  lastFrame = stamp;
  if (position >= replay.duration) pause();
  else requestAnimationFrame(animate);
}
function fillForm(settings) {
  for (const [key,value] of Object.entries(settings)) {
    const input = $("rules-form").elements.namedItem(key);
    if (typeof value === "boolean") input.checked = value;
    else input.value = value;
  }
}
async function load(settings) {
  pause(); $("rerun").disabled = true; $("reset").disabled = true;
  $("run-feedback").textContent = "Running the temporal engine…";
  try {
    const response = await fetch("/api/replay", settings ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(settings)} : {});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Replay failed");
    replay = result; position = Math.min(position,replay.duration); selected = null;
    $("scrubber").max = replay.duration;
    fillForm(replay.settings);
    $("run-stamp").textContent = "· Run complete";
    $("run-feedback").textContent = `${replay.summary.alerts} alerts over ${seconds(replay.duration)} · Fresh in-memory run`;
    $("error").hidden = true;
    if (exportURL) URL.revokeObjectURL(exportURL);
    exportURL = URL.createObjectURL(new Blob([JSON.stringify(replay,null,2)],{type:"application/json"}));
    $("export").href = exportURL; $("export").download = "pawception-mock-replay.json";
    render();
  } catch (error) {
    $("error").textContent = `Could not run the replay: ${error.message}. Check that the local Python server is running.`;
    $("error").hidden = false;
    $("run-feedback").textContent = "Run failed · Previous replay retained";
  } finally { $("rerun").disabled = false; $("reset").disabled = false; }
}
$("play").addEventListener("click",() => {
  if (!replay) return;
  if (playing) return pause();
  if (position >= replay.duration) position = 0;
  playing = true; selected = null; lastFrame = null;
  $("play-icon").textContent = "Ⅱ"; $("play").setAttribute("aria-label","Pause replay");
  requestAnimationFrame(animate);
});
$("restart").addEventListener("click",() => replay && seek(0));
$("scrubber").addEventListener("input",event => replay && seek(Number(event.target.value)));
$("next-result").addEventListener("click",() => {
  const next = replay?.snapshots.find(s => s.time > position);
  if (next) seek(next.time);
});
$("next-alert").addEventListener("click",() => {
  if (!replay) return;
  const next = finalSnapshot().alerts.find(a => a.detected_at > position);
  if (next) { selected = {kind:"alert",id:next.incident_id}; seek(next.detected_at,true); }
});
$("show-future").addEventListener("change",render);
document.addEventListener("click",event => {
  const item = event.target.closest("[data-kind]");
  if (!item || !replay) return;
  selected = {kind:item.dataset.kind,id:item.dataset.id};
  const known = Number(item.dataset.known);
  if (known > snapshot().time) {
    const available = replay.snapshots.find(s => s.time >= known);
    seek(available?.time ?? replay.duration,true);
  } else { pause(); render(); }
});
$("rules-form").addEventListener("submit",event => {
  event.preventDefault();
  load({threshold:Number($("threshold").value),repeated_window_seconds:Number($("repeated-window").value),merge_gap_seconds:Number($("merge-gap").value),missing_window_seconds:Number($("missing-window").value),include_gap:$("include-gap").checked});
});
$("reset").addEventListener("click",() => load({threshold:3,repeated_window_seconds:30,merge_gap_seconds:1,missing_window_seconds:10,include_gap:true}));
load();
