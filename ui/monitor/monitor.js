/* Setup preferences are browser-local. Python owns monitoring and inference. */
const SetupPreferences = (() => {
  const key = "pawception.setup.v1";
  const defaults = () => ({version:1,provider:"mock",window_seconds:60,overlap_seconds:0,merge_gap_seconds:1,safety_watch:true,
    safety_prompt:null,safety_exclusions:"",sam_snapshots:false,
    video_reasoning_effort:"medium",suggestion_reasoning_effort:"minimal",
    audio_model:"muse-spark-1.2-contributor",audio_reasoning_effort:"medium",capture_audio:false,
    model:"muse-spark-1.3-contributor",mock_delay_seconds:5,source:{kind:"sample"},removed_events:[],
    events:[{name:"Chewing the furniture",description:"A pet biting, chewing, or forcefully pulling furniture or cushions.",threshold_seconds:5,palette_index:0}]});
  const number = (value,min,max) => Number.isFinite(value) && value>=min && value<=max;
  const text = (value,max) => typeof value === "string" && value.trim().length>0 && value.length<=max;
  const models = ["muse-spark-1.3-contributor","muse-spark-1.3","muse-spark-1.2-contributor","muse-spark-1.2","muse-spark-1.1"];
  const inputModes = ["auto","visual","audio","both"];
  const effortsForModel = model => ["minimal","low","medium","high","xhigh",...(model === "muse-spark-1.3" ? ["max"] : [])];
  const effort = (value,model,fallback) => effortsForModel(model).includes(value) ? value
    : effortsForModel(model).includes(fallback) ? fallback : "xhigh";
  function eventDefinition(event) {
    if (!event || !text(event.name,80) || !text(event.description,1000) || !number(event.threshold_seconds,0.1,600)) return null;
    const inputs = {};
    if (event.input_mode !== undefined) {
      if (!inputModes.includes(event.input_mode)) return null;
      const resolved = event.input_mode === "auto" ? event.resolved_mode : event.input_mode;
      if (!["visual","audio","both"].includes(resolved)) return null;
      Object.assign(inputs,{input_mode:event.input_mode,resolved_mode:resolved});
    }
    return {name:event.name.trim(),description:event.description.trim(),threshold_seconds:event.threshold_seconds,palette_index:event.palette_index,...inputs};
  }

  function normalize(value, previous = defaults()) {
    if (!value || value.version !== 1 || !Array.isArray(value.events) || value.events.length>20) return null;
    const used = new Set();
    const events = [];
    for (const event of value.events) {
      const definition = eventDefinition(event);
      if (!definition) return null;
      let palette = event.palette_index;
      if (!Number.isInteger(palette) || palette<0 || palette>=20 || used.has(palette)) {
        palette = Array.from({length:20},(_,index)=>index).find(index=>!used.has(index));
      }
      used.add(palette);
      events.push({...definition,palette_index:palette});
    }
    const removed_events = [];
    const removedIds = new Set();
    if (value.removed_events !== undefined && !Array.isArray(value.removed_events)) return null;
    for (const record of value.removed_events || []) {
      const event = eventDefinition(record?.event);
      if (!event || !text(record.id,100) || removedIds.has(record.id) || !Number.isFinite(record.removed_at) || record.removed_at<0) return null;
      if (!Number.isInteger(event.palette_index) || event.palette_index<0 || event.palette_index>=20) event.palette_index = 0;
      removedIds.add(record.id);
      removed_events.push({id:record.id,removed_at:record.removed_at,event});
    }
    const window = number(value.window_seconds,1,120) ? value.window_seconds : previous.window_seconds;
    const overlap = number(value.overlap_seconds,0,119) && window-value.overlap_seconds>=1
      ? value.overlap_seconds : window-previous.overlap_seconds>=1 ? previous.overlap_seconds : 0;
    let source = previous.source;
    if (value.source?.kind === "sample") source = {kind:"sample"};
    if (value.source?.kind === "webcam") source = {kind:"webcam"};
    if (value.source?.kind === "file" && text(value.source.name,255)) source = {kind:"file",name:value.source.name};
    const model = models.includes(value.model) ? value.model : previous.model;
    const audio_model = models.includes(value.audio_model) ? value.audio_model : previous.audio_model;
    return {version:1,events,removed_events,provider:["mock","muse"].includes(value.provider) ? value.provider : previous.provider,
      window_seconds:window,overlap_seconds:overlap,
      merge_gap_seconds:number(value.merge_gap_seconds,0,30) ? value.merge_gap_seconds : previous.merge_gap_seconds,
      safety_watch:typeof value.safety_watch === "boolean" ? value.safety_watch : previous.safety_watch,
      sam_snapshots:false,
      safety_prompt:value.safety_prompt === null ? null : text(value.safety_prompt,6000) ? value.safety_prompt.trim() : previous.safety_prompt,
      safety_exclusions:typeof value.safety_exclusions === "string" && value.safety_exclusions.length<=2000 ? value.safety_exclusions.trim() : previous.safety_exclusions,
      model,audio_model,
      audio_reasoning_effort:effort(value.audio_reasoning_effort,audio_model,previous.audio_reasoning_effort),
      capture_audio:typeof value.capture_audio === "boolean" ? value.capture_audio : previous.capture_audio,
      video_reasoning_effort:effort(value.video_reasoning_effort,model,previous.video_reasoning_effort),
      suggestion_reasoning_effort:effort(value.suggestion_reasoning_effort,"muse-spark-1.3-contributor",previous.suggestion_reasoning_effort),
      mock_delay_seconds:number(value.mock_delay_seconds,0.1,300) ? value.mock_delay_seconds : previous.mock_delay_seconds,
      source};
  }

  function load(storage) {
    try { return normalize(JSON.parse(storage.getItem(key))); } catch { return null; }
  }

  function save(storage, value, previous) {
    const normalized = normalize(value,previous);
    if (!normalized) return null;
    try { storage.setItem(key,JSON.stringify(normalized)); return normalized; } catch { return null; }
  }

  function removeEvent(setup, index, id, removedAt) {
    if (!Number.isInteger(index) || !setup.events[index]) return null;
    return normalize({...setup,events:setup.events.filter((_,i)=>i!==index),
      removed_events:[...(setup.removed_events || []),{id,removed_at:removedAt,event:setup.events[index]}]});
  }

  function restoreEvent(setup, id) {
    const record = setup.removed_events?.find(record=>record.id===id);
    if (!record || setup.events.length>=20) return null;
    return normalize({...setup,events:[...setup.events,record.event],
      removed_events:setup.removed_events.filter(record=>record.id!==id)});
  }
  function deleteRemoved(setup,id) {
    return normalize({...setup,removed_events:(setup.removed_events||[]).filter(record=>record.id!==id)});
  }
  function emptyTrash(setup) {return normalize({...setup,removed_events:[]});}

  return {key,defaults,normalize,load,save,removeEvent,restoreEvent,deleteRemoved,emptyTrash,effortsForModel};
})();

if (typeof module !== "undefined" && module.exports) module.exports = SetupPreferences;
if (typeof document !== "undefined") (() => {
  "use strict";
  const el = id => document.getElementById(id);
  const escapeHTML = value => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const clock = seconds => `${Math.floor(seconds / 60).toString().padStart(2,"0")}:${Math.floor(seconds % 60).toString().padStart(2,"0")}`;
  const seconds = value => `${Number(value).toFixed(1).replace(/\.0$/, "")}s`;
  const liveVideo = el("live-video");
  const preview = el("preview");
  const review = new ActivityReview();
  let storage;
  try { storage = window.localStorage; } catch { /* Persistence status explains unavailable storage. */ }
  const savedSetup = SetupPreferences.load(storage);
  let preferences = savedSetup || SetupPreferences.defaults();
  let safetyDefaults, safetyPrompt = preferences.safety_prompt, safetyExclusions = preferences.safety_exclusions;
  let changedSinceLoad = false;
  let sourcePreference = preferences.source;
  let source, sessionSource, state, sampleURL, polling = false, starting = false, openingCamera = false;
  let replaySource;
  let sourceRevision = 0;
  let provider = preferences.provider;
  let previousSession = null;
  let alertSelection = null;
  let updatingSafety = false;
  const objectURLs = [], seen = new Set(), read = new Set();
  const drafts = preferences.events.map(event=>({...event}));
  const removed = preferences.removed_events.slice();
  const camera = new CameraCapture({onState:acceptState,onError:async message=>{
    try { if (state?.source_type === "webcam") acceptState(await api(`/api/monitor/${state.id}/stop`,{})); }
    catch { /* The backend also stops after 15 seconds without recording chunks. */ }
    if (sourcePreference.kind === "webcam") {
      source = null; preview.srcObject = null;
      renderSourceMode(); renderDrafts();
    }
    error(message);
  }});

  function savePreferences() {
    const saved = SetupPreferences.save(storage,{version:1,events:drafts,removed_events:removed,provider,source:sourcePreference,
      window_seconds:Number(el("window").value),overlap_seconds:Number(el("overlap").value),
      merge_gap_seconds:Number(el("merge-gap").value),
      safety_watch:el("safety-setup").checked,
      sam_snapshots:false,
      safety_prompt:safetyPrompt,safety_exclusions:safetyExclusions,
      audio_model:el("audio-model").value,audio_reasoning_effort:el("audio-reasoning").value,capture_audio:el("capture-audio").checked,
      video_reasoning_effort:el("video-reasoning").value,suggestion_reasoning_effort:el("suggestion-reasoning").value,
      model:el("model").value,mock_delay_seconds:Number(el("mock-delay").value)},preferences);
    if (saved) preferences = saved;
    changedSinceLoad = true;
    const message = saved ? "Events and settings saved in this browser." : "Settings could not be saved in this browser. Keep this page open to retain changes.";
    if (el("setup-save-status").textContent !== message) el("setup-save-status").textContent = message;
  }

  function restorePreferences() {
    el("window").value = preferences.window_seconds;
    el("overlap").value = preferences.overlap_seconds;
    el("merge-gap").value = preferences.merge_gap_seconds;
    el("safety-setup").checked = preferences.safety_watch;
    renderSafetySetup();
    el("model").value = preferences.model;
    renderReasoning(preferences.video_reasoning_effort);
    el("audio-model").value = preferences.audio_model;
    renderAudioReasoning(preferences.audio_reasoning_effort);
    el("capture-audio").checked = preferences.capture_audio;
    el("suggestion-reasoning").value = preferences.suggestion_reasoning_effort;
    el("mock-delay").value = preferences.mock_delay_seconds;
    setMode(preferences.provider,false);
  }

  async function api(path, value) {
    const response = await fetch(path, value === undefined ? {} : {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(value)
    });
    const result = await response.json();
    if (!response.ok) {
      const failure = new Error(result.error || "Could not update your monitor.");
      failure.status = response.status;
      throw failure;
    }
    return result;
  }

  function error(message) {
    el("error").textContent = message;
    el("error").hidden = !message;
  }

  function acceptState(next) {
    if (state?.id === next.id && state.revision > next.revision) return;
    state = next;
    renderLive();
  }

  const allEvents = () => [...(state?.events || []),...(state?.hazards || [])];
  const alertEvent = id => allEvents().find(event=>event.id===id);
  const reviewState = () => ({...state,events:allEvents()});

  function renderSafetySetup() {
    const enabled = el("safety-setup").checked;
    el("safety-setup").closest(".safety-watch-card").classList.toggle("is-off",!enabled);
    el("safety-setup-state").textContent = enabled ? "On" : "Off";
    el("safety-setup-icon").textContent = enabled ? "✓" : "×";
    el("safety-setup-help").textContent = enabled
      ? provider === "mock" ? "Scripted mock hazards for testing. No footage is analyzed or credits used."
        : "Checks the full scene, even without a pet in view. Alerts on first detection; continuing hazards are grouped."
      : "Safety alerts are off. Your custom events can still be monitored.";
  }
  el("safety-setup").addEventListener("change",()=>{renderSafetySetup();renderDrafts();savePreferences();});
  function editSafety(live) {
    if (!safetyDefaults || updatingSafety || starting) return;
    el("safety-dialog").dataset.live = String(live);
    el("hazard-prompt").value = (live ? state.safety_prompt : safetyPrompt) ?? safetyDefaults.safety_prompt;
    el("hazard-exclusions").value = (live ? state.safety_exclusions : safetyExclusions) || "";
    el("safety-edit-help").textContent = live
      ? "Changes apply to the next inference request and future sessions. In-flight requests finish using their previous instructions. Existing alerts stay available."
      : "Changes apply when you start your next session. Saved in this browser.";
    el("safety-mock-help").hidden = (live ? state.provider : provider) !== "mock";
    el("safety-edit-error").hidden = true;
    el("safety-dialog").showModal();
  }
  el("edit-safety-setup").addEventListener("click",()=>editSafety(false));
  el("edit-safety-live").addEventListener("click",()=>editSafety(true));
  el("restore-safety").addEventListener("click",()=>{
    el("hazard-prompt").value = safetyDefaults.safety_prompt;
    el("hazard-exclusions").value = "";
  });
  el("safety-form").addEventListener("submit",async event=>{
    event.preventDefault();
    if (updatingSafety) return;
    const prompt = el("hazard-prompt").value.trim();
    const settings = {safety_prompt:prompt === safetyDefaults.safety_prompt ? null : prompt,
      safety_exclusions:el("hazard-exclusions").value.trim()};
    if (!prompt) {
      el("safety-edit-error").textContent = "Enter a hazard prompt or restore defaults.";
      el("safety-edit-error").hidden = false;
      return;
    }
    updatingSafety = true;
    el("safety-form").querySelectorAll("button,textarea").forEach(control=>control.disabled=true);
    try {
      if (el("safety-dialog").dataset.live === "true") {
        acceptState(await api(`/api/monitor/${state.id}/safety`,settings));
      }
      safetyPrompt = settings.safety_prompt;
      safetyExclusions = settings.safety_exclusions;
      savePreferences();
      el("safety-dialog").close();
    } catch (failure) {
      el("safety-edit-error").textContent = failure.message;
      el("safety-edit-error").hidden = false;
    } finally {
      updatingSafety = false;
      el("safety-form").querySelectorAll("button,textarea").forEach(control=>control.disabled=false);
      if (state) renderLive();
    }
  });
  el("safety-dialog").addEventListener("cancel",event=>{if (updatingSafety) event.preventDefault();});
  el("safety-live").addEventListener("change",async()=>{
    if (updatingSafety || !state || state.status !== "live") return;
    const enabled = el("safety-live").checked;
    updatingSafety = true;
    el("safety-live").disabled = true;
    try {
      acceptState(await api(`/api/monitor/${state.id}/safety`,{enabled}));
      el("safety-setup").checked = state.safety_watch;
      renderSafetySetup(); savePreferences(); error("");
    } catch (failure) { el("safety-live").checked = state.safety_watch; error(failure.message); }
    finally { updatingSafety = false; renderLive(); }
  });

  function page(name) {
    el("setup-view").hidden = name !== "setup";
    el("live-view").hidden = name !== "live";
    el("setup-nav").classList.toggle("selected", name === "setup");
    el("live-nav").classList.toggle("selected", name === "live");
  }

  const inputLabel = event => `${event.input_mode === "auto" ? "Smart Routing → " : ""}${({visual:"Visual",audio:"Audio",both:"Visual + Audio"})[event.resolved_mode || event.input_mode || "visual"]}`;

  function cards(events, live = false) {
    return events.map((event, index) => `<article class="event-card" style="${eventStyle(event,index)}">
      <span class="event-icon" aria-hidden="true">${String(index + 1).padStart(2,"0")}</span>
      <div class="event-copy"><h3>${escapeHTML(event.name)}</h3><p>${escapeHTML(event.description)}</p>
        ${live ? `<p class="event-threshold"><span class="event-input-label">${escapeHTML(inputLabel(event))}</span> · ◷ Alert after ${seconds(event.threshold_seconds)} detected activity</p>`
          : `<p class="event-threshold"><span class="event-input-label">${escapeHTML(inputLabel(event))}</span></p>
            <label class="event-duration">Alert after <input type="number" min="0.1" max="600" step="0.1" value="${event.threshold_seconds}" data-threshold="${index}" aria-label="Alert duration in seconds for ${escapeHTML(event.name)}" required> seconds of detected activity</label>`}
        ${live ? `<span class="event-state">${event.input_issue ? `<strong>${escapeHTML(event.input_issue)}</strong><br>` : ""}${event.analyzed_windows ? `Longest detected stretch: ${seconds(event.longest_detected_seconds)}` : "Waiting for first analysis"}<br>Active from ${clock(event.active_from)}</span>` : ""}
      </div>${live ? "" : `<button class="event-remove" data-remove="${index}" aria-label="Remove ${escapeHTML(event.name)}">×</button>`}
    </article>`).join("");
  }

  function renderDrafts() {
    el("setup-events").innerHTML = cards(drafts) || '<p class="card-intro">Add your first event to get started.</p>';
    el("event-count").textContent = `${drafts.length} ${drafts.length === 1 ? "event" : "events"}`;
    el("add-setup-event").disabled = drafts.length >= 20;
    el("go-live").disabled = starting || openingCamera || !!previousSession || !source?.duration || !drafts.length && !el("safety-setup").checked;
    for (const id of ["mode-mock","mode-muse","window","overlap","merge-gap","model","video-reasoning","audio-model","audio-reasoning","suggestion-reasoning","capture-audio","file","add-setup-event"]) {
      el(id).disabled = starting || id === "add-setup-event" && drafts.length >= 20;
    }
    el("mock-delay").disabled = starting || provider === "muse";
    el("sample").disabled = starting || !sampleURL;
    for (const id of ["source-replay","source-webcam","enable-camera"]) el(id).disabled = starting;
    el("enable-camera").disabled = starting || openingCamera;
    el("safety-setup").disabled = starting;
    el("edit-safety-setup").disabled = starting || updatingSafety || !safetyDefaults;
    el("setup-events").querySelectorAll("button,input").forEach(control => { control.disabled = starting; });
    el("removed-events-section").hidden = !removed.length;
    el("removed-count").textContent = removed.length;
    el("removed-events").innerHTML = removed.slice().reverse().map(record=>`<article class="removed-event">
      <div class="event-copy"><h3>${escapeHTML(record.event.name)}</h3><p>${escapeHTML(record.event.description)}</p>
        <p class="event-threshold">Alert after ${seconds(record.event.threshold_seconds)} detected activity</p></div>
      <button class="restore-event" data-restore="${escapeHTML(record.id)}" ${starting || drafts.length>=20 ? "disabled" : ""}>Restore</button>
      <button class="delete-removed-event" data-delete-removed="${escapeHTML(record.id)}" ${starting ? 'disabled' : ''}>Delete forever</button>
    </article>`).join("");
    el("restore-limit").hidden = drafts.length<20;
    el("empty-trash").disabled=starting;
  }

  function selectSource(url, name, file = null, persist = true) {
    sourceRevision++;
    camera.stop();
    preview.srcObject = null;
    source = {url, name, file,kind:"video"};
    replaySource = source;
    sourcePreference = file ? {kind:"file",name:file.name} : {kind:"sample"};
    preview.src = url;
    renderSourceMode();
    renderSafetySetup();
    el("source-name").textContent = name;
    el("source-detail").textContent = "Loading video…";
    el("source-duration").textContent = "—";
    renderDrafts();
    if (persist) savePreferences();
  }

  preview.addEventListener("loadedmetadata", () => {
    if (sourcePreference.kind === "webcam") return;
    const duration = preview.duration;
    if (!Number.isFinite(duration) || duration < 0.1 || duration > 7200) {
      error("Choose a playable video up to two hours long.");
      return;
    }
    source.duration = duration;
    el("source-detail").textContent = `${clock(duration)} · Plays once at normal speed`;
    el("source-duration").textContent = clock(duration);
    error("");
    renderDrafts();
  });
  preview.addEventListener("error", () => {
    if (source) {
      source.duration = null;
      error("This video could not be played. Try an MP4 supported by your browser.");
      renderDrafts();
    }
  });
  el("file").addEventListener("change", event => {
    const file = event.target.files[0];
    if (!file) return;
    const url = URL.createObjectURL(file);
    objectURLs.push(url);
    selectSource(url, file.name, file);
  });
  el("sample").addEventListener("click", () => selectSource(sampleURL, "Kitten stuck · Sample"));

  function renderSourceMode() {
    const webcam = sourcePreference.kind === "webcam";
    for (const [id,selected] of [["source-webcam",webcam],["source-replay",!webcam]]) {
      el(id).classList.toggle("selected",selected);
      el(id).setAttribute("aria-pressed",String(selected));
    }
    el("video-actions").hidden = webcam;
    el("camera-actions").hidden = !webcam;
    el("camera-placeholder").hidden = !webcam || !!camera.stream;
    preview.style.visibility = webcam && !camera.stream ? "hidden" : "visible";
    el("preview-label").textContent = webcam ? "CAMERA PREVIEW" : "VIDEO SOURCE";
    if (webcam) {
      el("source-name").textContent = "Webcam";
      el("source-detail").textContent = camera.stream ? `Camera ready · 480p · ${source?.capture_audio ? "Microphone on" : "Microphone off"}` : "Enable your camera, then go live. Include the microphone for audio events.";
      el("source-duration").textContent = camera.stream ? "READY" : "OFF";
      el("enable-camera").textContent = camera.stream ? "Reconnect camera" : el("capture-audio").checked ? "Enable camera and microphone" : "Enable camera";
    }
    el("source-privacy").textContent = webcam
      ? `Camera recordings are saved to the local backend${provider === "muse" ? "; completed windows are sent to Meta" : ". Mock mode uses scripted observations"}. Keep this page open. Sessions stop after two hours or 1 GB.`
      : provider === "mock" ? "Local files stay in this browser in mock mode. Live replay has no pause or seek controls."
        : "Videos up to 200 MB are copied to the local backend. Captured windows are sent to Meta; temporary API uploads are deleted after use.";
    el("go-live-note").textContent = webcam ? "480p · Stop releases camera and microphone" : provider === "mock" ? "Webcam access is not requested." : `${el("video-reasoning").value} reasoning · Real API calls · No automatic retries`;
  }
  el("source-webcam").addEventListener("click",()=>{
    if (starting) return;
    sourceRevision++;
    camera.stop();
    source = null;
    sourcePreference = {kind:"webcam"};
    preview.pause();
    preview.srcObject = null;
    preview.removeAttribute("src");
    preview.load();
    renderSourceMode(); renderDrafts(); savePreferences(); error("");
  });
  el("source-replay").addEventListener("click",()=>{
    if (starting) return;
    if (replaySource) selectSource(replaySource.url,replaySource.name,replaySource.file);
    else if (sampleURL) selectSource(sampleURL,"Kitten stuck · Sample");
  });
  el("enable-camera").addEventListener("click",async()=>{
    if (openingCamera) return;
    openingCamera = true;
    const revision = ++sourceRevision;
    source = null;
    renderDrafts();
    try {
      const stream = await camera.open({audio:el("capture-audio").checked});
      if (revision !== sourceRevision) { camera.stop(); return; }
      source = {kind:"webcam",name:"Webcam",duration:7200,stream,capture_audio:stream.getAudioTracks().length>0};
      preview.srcObject = stream;
      await preview.play();
      error("");
    } catch (failure) {
      if (revision === sourceRevision) { camera.stop(); source = null; error(failure.message); }
    }
    finally { openingCamera = false; renderSourceMode(); renderDrafts(); }
  });

  function renderReasoning(selected = el("video-reasoning").value) {
    const levels = SetupPreferences.effortsForModel(el("model").value);
    el("video-reasoning").innerHTML = levels.map(level=>`<option value="${level}">${level === "xhigh" ? "Extra high" : level[0].toUpperCase()+level.slice(1)}</option>`).join("");
    el("video-reasoning").value = levels.includes(selected) ? selected : "xhigh";
  }

  function renderAudioReasoning(selected = el("audio-reasoning").value) {
    const levels = SetupPreferences.effortsForModel(el("audio-model").value);
    el("audio-reasoning").innerHTML = levels.map(level=>`<option value="${level}">${level === "xhigh" ? "Extra high" : level[0].toUpperCase()+level.slice(1)}</option>`).join("");
    el("audio-reasoning").value = levels.includes(selected) ? selected : "xhigh";
  }
  function renderAudioSettings() {
    const shared=el("audio-model").value===el("model").value;
    el("audio-model-warning").hidden = !el("audio-model").value.startsWith("muse-spark-1.3");
    el("audio-model-help").textContent = shared
      ? "Matching models: one combined request and upload when both inputs are needed, using the higher reasoning level. Safety Watch uses visual evidence."
      : "Audio events send sound only. Visual + Audio events send video with sound to the audio model. Safety Watch uses the visual model.";
  }

  function setMode(mode, persist = true) {
    if (starting) return;
    provider = mode;
    const mock = mode === "mock";
    el("mode-mock").classList.toggle("selected",mock);
    el("mode-muse").classList.toggle("selected",!mock);
    el("mode-mock").setAttribute("aria-pressed",mock);
    el("mode-muse").setAttribute("aria-pressed",!mock);
    el("mode-label").textContent = mock ? "Mock mode" : "Muse mode";
    el("mode-note-title").textContent = mock ? "A preview of your pet’s monitor" : "A live connection to Muse";
    el("mode-note-copy").textContent = mock ? "Real video. Scripted observations. No AI requests or credits used." : "Captured video or audio windows and event descriptions are sent to Meta for analysis.";
    el("mode-help").textContent = mock ? "Scripted observations for testing the interface. No credentials or credits are used." : "Uses MODEL_API_KEY from your local .env file. Go Live submits real inference requests and uses API credits. Stop the session to change modes.";
    el("mock-delay").disabled = !mock;
    el("mock-delay-field").hidden = !mock;
    el("model-help").textContent = mock ? "Model selection is saved for the session. Mock mode uses scripted observations for every model." : "The selected model analyzes each captured window at the selected reasoning level, including Safety Watch. Higher effort can increase latency and cost. Changes apply to the next session.";
    el("source-privacy").textContent = mock ? "Local files stay in this browser in mock mode. Live replay has no pause or seek controls." : "Videos up to 200 MB are copied to the local backend. Captured windows are sent to Meta; temporary API uploads are deleted after use.";
    el("go-live").innerHTML = `<span class="live-dot"></span>${mock ? "Go live" : "Go live with Muse"}<span class="button-arrow">↗</span>`;
    el("go-live").toggleAttribute("data-muse",!mock);
    el("go-live-note").textContent = mock ? "Webcam access is not requested." : `${el("video-reasoning").value} reasoning · Real API calls · No automatic retries`;
    renderSourceMode();
    renderSafetySetup();
    renderAudioSettings();
    if (persist) savePreferences();
  }
  el("mode-mock").addEventListener("click", () => setMode("mock"));
  el("mode-muse").addEventListener("click", () => setMode("muse"));

  const descriptionSuggestions = new DescriptionSuggestions({onChange:next=>{
    const choosing = next.phase === "choosing";
    el("event-editor").hidden = choosing;
    el("save-event").hidden = choosing;
    el("event-suggestions").hidden = !choosing;
    el("suggest-description").disabled = next.phase === "loading";
    el("suggest-description").textContent = next.phase === "loading" ? "Polishing prompt…" : "Prompt Polish";
    el("suggestion-status").textContent = next.message;
    el("suggestion-status").hidden = !next.message;
    el("suggestion-options").innerHTML = next.options.map((option,index)=>`<button type="button" class="suggestion-option" data-suggestion="${index}">
      <strong>${escapeHTML(option.label)}</strong><span>${escapeHTML(option.description)}</span><small>Use and edit ↗</small></button>`).join("");
    if (choosing) el("suggestion-options").querySelector("button")?.focus();
  }});
  el("suggest-description").addEventListener("click",()=>{
    el("event-error").hidden = true;
    descriptionSuggestions.generate({name:el("event-name").value,description:el("event-description").value,reasoning_effort:el("suggestion-reasoning").value},
      el("event-dialog").dataset.live === "true" ? state.provider : provider);
  });
  el("suggestion-options").addEventListener("click",event=>{
    const button = event.target.closest("[data-suggestion]");
    if (!button) return;
    const description = descriptionSuggestions.choose(Number(button.dataset.suggestion));
    if (description === null) return;
    eventDraftRevision++;
    el("event-description").value = description;
    el("event-description").focus();
  });
  el("reject-suggestions").addEventListener("click",()=>{
    descriptionSuggestions.reset("Your wording is unchanged. You can keep editing.");
    el("event-description").focus();
  });
  for (const id of ["event-name","event-description"]) el(id).addEventListener("input",()=>descriptionSuggestions.reset());
  let eventDraftRevision = 0;
  function updateEventModelIcons() {
    const real = (el("event-dialog").dataset.live === "true" ? state?.provider : provider) === "muse";
    el("suggest-description").toggleAttribute("data-muse",real);
    el("save-event").toggleAttribute("data-muse",real && el("event-input-mode").value === "auto");
  }
  el("event-input-mode").addEventListener("change",updateEventModelIcons);
  for (const name of ["input","change"]) el("event-form").addEventListener(name,()=>eventDraftRevision++);
  el("event-dialog").addEventListener("close",()=>{ eventDraftRevision++; descriptionSuggestions.reset(); });

  function openEventForm(live) {
    eventDraftRevision++;
    el("event-form").reset();
    descriptionSuggestions.reset();
    el("event-dialog").dataset.live = live ? "true" : "false";
    updateEventModelIcons();
    el("event-error").hidden = true;
    const mode = live ? state.provider : provider;
    el("suggestion-cost").textContent = mode === "mock" ? "Mock templates · No credits" : "Muse Spark 1.3 Contributor · Text only · Uses credits";
    el("event-help").textContent = live
      ? "Joins the next request sent for analysis. Its timer starts now; earlier footage cannot trigger an alert."
      : provider === "mock" ? "Mock mode generates scripted activity for each event. It does not interpret the description or video."
        : "Choose the inputs for this event, or let Smart Routing choose before saving.";
    el("event-dialog").showModal();
  }
  el("add-setup-event").addEventListener("click", () => openEventForm(false));
  el("add-live-event").addEventListener("click", () => openEventForm(true));
  function saveEventThreshold(event) {
    const input = event.target.closest("[data-threshold]");
    if (!input || starting) return;
    if (!input.checkValidity()) {
      if (event.type === "change") input.reportValidity();
      return;
    }
    drafts[Number(input.dataset.threshold)].threshold_seconds = Number(input.value);
    savePreferences();
  }
  el("setup-events").addEventListener("input", saveEventThreshold);
  el("setup-events").addEventListener("change", saveEventThreshold);
  el("setup-events").addEventListener("click", event => {
    const button = event.target.closest("[data-remove]");
    if (!button || starting) return;
    const id = crypto.randomUUID();
    const next = SetupPreferences.removeEvent({...preferences,events:drafts,removed_events:removed},Number(button.dataset.remove),id,Date.now());
    if (!next) return;
    updateEventLists(next);
    const notice = document.createElement("div");
    notice.className = "toast removal-toast";
    notice.dataset.removedId = id;
    notice.innerHTML = `<span><strong>${escapeHTML(next.removed_events.at(-1).event.name)} removed</strong><small>Available in Trash</small></span><button class="restore-event" data-restore="${id}">Undo</button>`;
    el("toasts").append(notice);
    setTimeout(()=>notice.remove(),10000);
  });
  function updateEventLists(next) {
    drafts.splice(0,drafts.length,...next.events);
    removed.splice(0,removed.length,...next.removed_events);
    renderDrafts();
    savePreferences();
  }
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-restore]");
    if (!button || starting) return;
    const next = SetupPreferences.restoreEvent({...preferences,events:drafts,removed_events:removed},button.dataset.restore);
    if (!next) {
      if (drafts.length>=20) error("Remove an active event to make room before restoring. You can watch up to 20 events.");
      return;
    }
    updateEventLists(next);
    document.querySelectorAll("[data-removed-id]").forEach(notice=>{
      if (notice.dataset.removedId===button.dataset.restore) notice.remove();
    });
    error("");
  });
  el('removed-events').addEventListener('click',event=>{
    const button=event.target.closest('[data-delete-removed]');
    if(!button||starting||!confirm('Delete this event definition forever? Past alerts and recordings will remain.'))return;
    const next=SetupPreferences.deleteRemoved({...preferences,events:drafts,removed_events:removed},button.dataset.deleteRemoved);
    if(next)updateEventLists(next);
    document.querySelectorAll('[data-removed-id]').forEach(notice=>{if(notice.dataset.removedId===button.dataset.deleteRemoved)notice.remove();});
  });
  el('empty-trash').addEventListener('click',()=>{
    if(starting||!confirm('Empty the event trash? Definitions cannot be restored. Past alerts and recordings will remain.'))return;
    const next=SetupPreferences.emptyTrash({...preferences,events:drafts,removed_events:removed});
    if(next)updateEventLists(next);
    document.querySelectorAll('[data-removed-id]').forEach(notice=>notice.remove());
  });
  el("event-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (descriptionSuggestions.state.phase === "choosing") return;
    const value = {name:el("event-name").value.trim(), description:el("event-description").value.trim(),
      threshold_seconds:Number(el("event-threshold").value),
      input_mode:el("event-input-mode").value,
      palette_index:Array.from({length:20},(_,i)=>i).find(i=>!drafts.some(event=>event.palette_index===i))};
    if (!value.name || !value.description) {
      el("event-error").textContent = "Add a name and description for this event.";
      el("event-error").hidden = false;
      return;
    }
    el("save-event").disabled = true;
    const revision = eventDraftRevision;
    const liveSession = el("event-dialog").dataset.live === "true" ? state.id : null;
    try {
      if (value.input_mode === "auto") {
        el("save-event").textContent = "Choosing inputs…";
        const route = await api("/api/monitor/route",{name:value.name,description:value.description,
          provider:el("event-dialog").dataset.live === "true" ? state.provider : provider,
          reasoning_effort:el("suggestion-reasoning").value});
        if (revision !== eventDraftRevision || !el("event-dialog").open) return;
        value.resolved_mode=route.resolved_mode;
      } else value.resolved_mode=value.input_mode;
      if (liveSession) {
        acceptState(await api(`/api/monitor/${liveSession}/event`, value));
      }
      drafts.push(value);
      renderDrafts();
      savePreferences();
      el("event-dialog").close();
    } catch (failure) {
      if (revision !== eventDraftRevision || !el("event-dialog").open) return;
      el("event-error").textContent = failure.message;
      el("event-error").hidden = false;
    } finally { el("save-event").disabled = false; el("save-event").textContent = "Add event"; }
  });
  document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => el(button.dataset.close).close()));

  function alertCards(alerts) {
    return alerts.slice().reverse().map(alert => `<button class="alert-item ${read.has(alert.id) ? "is-viewed" : "is-unread"}" style="${eventStyle(alertEvent(alert.event_id))}" data-alert="${alert.id}">
      <span class="alert-time">${clock(alert.at)}</span>${snapshotImage(alert)}<span><strong>${escapeHTML(alert.event_name)}</strong>
      ${alert.source === "safety" ? '<span class="safety-alert-label">SAFETY WATCH</span>' : ""}
      <span class="alert-read-label">${read.has(alert.id) ? "✓ Viewed" : "● Unread"}</span>
      <small>${alert.source === "safety" ? "Immediate hazard alert" : `${seconds(alert.threshold_seconds)} detected activity`} · ${state.provider === "muse" ? "Muse detection" : "Mock observation"}<br>Received ${clock(alert.available_after_run_seconds)} into the session</small></span>
      <span class="arrow">↗</span></button>`).join("");
  }

  function snapshotImage(alert) {
    const url = alert.visual_evidence?.snapshot_url;
    return url ? `<img class="alert-snapshot" src="${escapeHTML(url)}" alt="Snapshot of ${escapeHTML(alert.event_name)}">` : "";
  }

  function renderSnapshotDetail(alert) {
    const root = el("evidence-snapshot");
    if (!root) return;
    const visual = alert.visual_evidence;
    root.hidden = !visual;
    if (!visual || root.dataset.signature === JSON.stringify(visual)) return;
    root.dataset.signature = JSON.stringify(visual);
    const view = root.dataset.view || "zoom";
    const url = visual[view+"_url"] || visual.original_url;
    const note = visual.zoom_url && visual.status !== "no_matches"
      ? `Frame at ${clock(visual.source_seconds)}${view==='highlighted' ? " · Green: pet · Amber: object" : ""}${visual.status==='processing' ? " · Adding highlights…" : visual.status==='unavailable' ? " · Some highlights unavailable" : ""}`
      : visual.status==='queued' || visual.status==='processing' ? "Preparing the close-up…"
      : visual.status==='unavailable' ? "Close-up unavailable. Review the original footage below."
      : "Showing the source frame; no object highlights are available.";
    root.innerHTML = `<div class="snapshot-heading"><strong>At a glance</strong><div>${["zoom","highlighted","original"].map(mode=>`<button type="button" data-snapshot-view="${mode}" ${visual[mode+"_url"] ? "" : "disabled"} aria-pressed="${view===mode}">${{zoom:"Close-up",highlighted:"Highlights",original:"Original"}[mode]}</button>`).join("")}</div></div>
      ${url ? `<img src="${escapeHTML(url)}" alt="${view==='highlighted' ? 'Pet and object highlights' : 'Evidence snapshot'}">` : ""}
      <p class="field-help">${escapeHTML(note)}</p>`;
    root.querySelectorAll("[data-snapshot-view]").forEach(button=>button.addEventListener("click",()=>{
      root.dataset.view = button.dataset.snapshotView;
      delete root.dataset.signature;
      renderSnapshotDetail(alert);
    }));
  }

  function showAlertList() {
    el("evidence-video")?.pause();
    alertSelection = null;
    el("alert-detail").innerHTML = `<div class="alert-feed">${state?.alerts.length ? alertCards(state.alerts) : '<div class="empty-state"><strong>Nothing to review yet.</strong>Alerts appear when a hazard is detected or a custom event reaches its duration threshold.</div>'}</div>`;
    renderUnread();
  }

  function renderUnread() {
    const count = state?.alerts.filter(alert => !read.has(alert.id)).length || 0;
    el("unread").textContent = count;
    el("unread").hidden = count === 0;
    el("notifications").setAttribute("aria-label", `Notifications${count ? `, ${count} unread` : ""}`);
  }

  function showAlert(id) {
    const alert = state?.alerts.find(item => item.id === id);
    if (!alert) return;
    alertSelection = id;
    const wasViewed = read.has(id);
    read.add(id);
    renderAlertFeed();
    review.update(reviewState(), sessionSource, read);
    renderUnread();
    const start = Math.max(0, alert.evidence_span.start - 2);
    const webcam = state.source_type === "webcam";
    const end = Math.min(state.duration, state.captured_seconds, alert.evidence_span.end + 2,webcam ? start+120 : Infinity);
    const offset = webcam ? start : 0;
    el("alert-detail").innerHTML = `<button class="back-to-alerts" id="all-alerts">← All alerts</button>
      <span class="evidence-badge">${alert.source === "safety" ? "SAFETY WATCH · " : ""}${state.provider === "muse" ? "MUSE DETECTION" : "SCRIPTED MOCK OBSERVATION"}</span>
      <h3 class="evidence-heading" style="${eventStyle(alertEvent(alert.event_id))}margin-top:18px">${escapeHTML(alert.event_name)}</h3>
      <p class="evidence-copy">${escapeHTML(alert.evidence)} The preview below shows the corresponding section of your source video.</p>
      <div id="evidence-snapshot" class="evidence-snapshot" hidden></div>
      <div class="evidence-wrap"><video id="evidence-video" playsinline preload="auto" aria-label="Alert evidence video"></video></div>
      <div class="evidence-controls"><button id="evidence-play" aria-label="Play evidence">▶</button><input id="evidence-position" type="range" min="0" max="${end-start}" step="0.05" value="0" aria-label="Evidence position"><span id="evidence-clock">${clock(start)}</span></div>
      <p class="field-help">Evidence preview · ${clock(start)}–${clock(end)} · Live replay continues independently</p>
      <div class="evidence-stats"><div><span>Event interval</span><strong>${clock(alert.evidence_span.start)}–${clock(alert.evidence_span.end)}</strong></div><div><span>Alert policy</span><strong>${alert.source === "safety" ? "First detection" : seconds(alert.threshold_seconds)}</strong></div><div><span>Triggered in video</span><strong>${clock(alert.at)}</strong></div><div><span>Notification received</span><strong>${clock(alert.available_after_run_seconds)} into session</strong></div></div>`;
    const video = el("evidence-video");
    renderSnapshotDetail(alert);
    video.src = webcam ? cameraMediaURL(state.id,start,end) : sessionSource.url;
    video.addEventListener("loadedmetadata", () => { video.currentTime = start-offset; }, {once:true});
    const updateButton = () => {
      el("evidence-play").textContent = video.paused ? "▶" : "Ⅱ";
      el("evidence-play").setAttribute("aria-label", video.paused ? "Play evidence" : "Pause evidence");
    };
    video.addEventListener("play", updateButton);
    video.addEventListener("pause", updateButton);
    video.addEventListener("timeupdate", () => {
      const position = video.currentTime+offset;
      if (position >= end) {
        video.pause();
        if (position > end + 0.05) video.currentTime = end-offset;
      }
      if (position < start) video.currentTime = start-offset;
      el("evidence-position").value = Math.max(0, position-start);
      el("evidence-clock").textContent = clock(position);
    });
    el("evidence-play").addEventListener("click", async () => {
      if (!video.paused) { video.pause(); return; }
      if (video.currentTime+offset >= end - 0.1) video.currentTime = start-offset;
      try { await video.play(); } catch { error("Evidence playback could not start. Try again."); }
    });
    el("evidence-position").addEventListener("input", event => { video.currentTime = start-offset + Number(event.target.value); });
    el("all-alerts").addEventListener("click", showAlertList);
    if (!el("alerts-dialog").open) el("alerts-dialog").showModal();
    const requestedSession = state.id;
    if (!wasViewed) api(`/api/monitor/${requestedSession}/viewed`, {alert_id:id}).then(next => {
      if (state.id === requestedSession) acceptState(next);
    }).catch(failure => {
      if (state.id !== requestedSession) return;
      read.delete(id);
      renderAlertFeed(); renderUnread(); review.update(reviewState(),sessionSource,read);
      error(`Could not save the alert's viewed status: ${failure.message}`);
    });
  }

  el("alerts-dialog").addEventListener("close", () => el("evidence-video")?.pause());
  el("notifications").addEventListener("click", () => { showAlertList(); el("alerts-dialog").showModal(); });
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-alert]");
    if (button) showAlert(button.dataset.alert);
  });

  function toast(alert) {
    const button = document.createElement("button");
    button.className = "toast";
    button.style.cssText = eventStyle(alertEvent(alert.event_id));
    button.dataset.alert = alert.id;
    button.innerHTML = `<span class="toast-picture">${snapshotImage(alert) || '<span class="toast-icon">◇</span>'}</span><span><strong>${escapeHTML(alert.event_name)}</strong><small>${alert.source === "safety" ? `Safety watch · ${state.provider === "muse" ? "Potential danger detected" : "Scripted mock hazard"}` : `${seconds(alert.threshold_seconds)} of ${state.provider === "muse" ? "activity" : "scripted activity"} detected`}.<br>Review the moment ↗</small></span>`;
    el("toasts").append(button);
    setTimeout(() => button.remove(), alert.visual_evidence ? 30_000 : 10_000);
  }

  function renderAlertFeed() {
    const active = ["live", "finishing"].includes(state.status);
    const noValidAnalysis = state.failed_windows?.length && !state.windows.some(job=>["completed","partial"].includes(job.status));
    el("alert-feed").innerHTML = alertCards(state.alerts) || `<div class="empty-state"><strong>${noValidAnalysis ? "No valid analysis results yet." : active ? "A little quiet for now." : "No alerts in this session."}</strong>${noValidAnalysis ? "Processing failed. The Processing track shows which windows failed." : active ? "Alerts appear when a hazard is detected or custom activity reaches its duration threshold." : "Review the processing status above for any skipped footage."}</div>`;
    if (el("alerts-dialog").open && !alertSelection) showAlertList();
    if (alertSelection) {
      const selected = state.alerts.find(alert=>alert.id===alertSelection);
      if (selected) renderSnapshotDetail(selected);
    }
    for (const toast of el("toasts").querySelectorAll("[data-alert]")) {
      const alert = state.alerts.find(item=>item.id===toast.dataset.alert);
      const picture = toast.querySelector(".toast-picture");
      if (alert?.visual_evidence?.snapshot_url && picture && picture.dataset.url!==alert.visual_evidence.snapshot_url) {
        picture.innerHTML = snapshotImage(alert);
        picture.dataset.url = alert.visual_evidence.snapshot_url;
      }
    }
  }

  function renderLive() {
    (state.viewed_alert_ids || []).forEach(id => read.add(id));
    const active = state.status === "live" || state.status === "finishing";
    el("safety-live").closest(".safety-live").classList.toggle("is-off",!state.safety_watch);
    if (!updatingSafety) el("safety-live").checked = !!state.safety_watch;
    el("safety-live").disabled = updatingSafety || state.status !== "live";
    el("edit-safety-live").disabled = !safetyDefaults || updatingSafety || state.status !== "live";
    el("safety-live-state").textContent = state.safety_watch ? "On" : "Off";
    el("safety-live-icon").textContent = state.safety_watch ? "✓" : "×";
    el("safety-live-help").textContent = state.safety_watch
      ? `${state.provider === "mock" ? "Scripted mock hazards" : "Muse safety checks"} · ${safetyTimelineStatus(state)}. Continuing detections share one alert.`
      : state.events.length ? "Off · No new safety alerts. Custom events continue; previous evidence remains available."
        : "Off · Monitoring is paused. Enable Safety watch or add a custom event. Footage continues recording.";
    el("setup-nav").disabled = active;
    el("live-nav").disabled = false;
    el("stop").hidden = !active;
    el("back-setup").hidden = active;
    el("add-live-event").disabled = state.status !== "live" || state.events.length >= 20;
    const mock = state.provider !== "muse";
    el("mode-label").textContent = mock ? "Mock mode" : "Muse mode";
    el("observation-badge").textContent = mock ? "MOCK OBSERVATIONS" : "MUSE DETECTIONS";
    const models = `Video: ${state.model} (${state.video_reasoning_effort || "medium"}) · Audio: ${state.audio_model} (${state.audio_reasoning_effort || "medium"})`;
    el("live-subtitle").textContent = `${seconds(state.window_seconds)} windows · ${seconds(state.overlap_seconds)} overlap · ${seconds(state.merge_gap_seconds || 0)} event gap tolerance · ${mock ? "Scripted mock observations" : models}`;
    const webcam = state.source_type === "webcam";
    el("session-status").textContent = ({live:webcam ? "Live camera" : "Live replay",finishing:"Finishing analysis",completed:"Session complete",stopped:"Monitoring stopped",error:"Muse needs attention"})[state.status];
    el("video-status").textContent = state.status === "live" ? webcam ? "LIVE CAMERA" : "LIVE REPLAY" : state.status === "stopped" ? "STOPPED" : "CAPTURE ENDED";
    el("live-source").textContent = state.source_name;
    el("live-clock").textContent = clock(state.captured_seconds);
    el("captured").textContent = clock(state.captured_seconds);
    const completed = state.windows.filter(job => ["completed","partial"].includes(job.status));
    el("analyzed").textContent = completed.length ? clock(state.analyzed_through)
      : state.active_window?.checks_finished ? 'Partial results ready' : "Awaiting result";
    el("lag").textContent = seconds(state.lag_seconds);
    el("processing-title").textContent = state.active_window ? "Analyzing a window" : state.status === "live" ? !state.events.length && !state.safety_watch ? "Monitoring paused" : "Collecting footage" : state.status === "finishing" ? "Finishing analysis" : state.status === "stopped" ? "Processing stopped" : "Analysis finished";
    el("queue-count").textContent = `${state.queue_size} / ${state.max_pending} waiting`;
    const collecting = state.collecting_start;
    el("buffer-label").textContent = collecting === null || !active ? "Capture finished" : "Filling the next window";
    const collected = collecting === null ? 0 : Math.max(0, state.captured_seconds - collecting);
    el("buffer-progress").value = collecting === null ? 1 : Math.min(1, collected / state.window_seconds);
    el("buffer-detail").textContent = collecting === null ? "" : `${seconds(collected)} / ${seconds(state.window_seconds)}`;
    el("processing-detail").textContent = state.active_window
      ? `Processing ${clock(state.active_window.start_seconds)}–${clock(state.active_window.end_seconds)} · ${mock ? `${seconds(state.mock_delay_seconds)} simulated work` : ({preparing:"Preparing clip",uploading:"Uploading to Meta",analyzing:"Muse inference",cleaning_up:"Cleaning up API upload"})[state.active_window.phase] || "Muse processing"} · One worker`
      : `${completed.length} ${completed.length === 1 ? "window" : "windows"} analyzed · Fixed windows · ${mock ? "No model calls" : "Muse mode"}`;
    if (state.active_window?.checks_total) {
      const ready = state.active_window.checks_finished || 0;
      el('processing-detail').textContent += ` · ${ready}/${state.active_window.checks_total} checks finished${ready ? ' · Results appear as each check finishes' : ''}`;
    }
    const failures = [...(state.failed_windows || []),...(state.partial_windows || [])];
    if (failures.length && state.status === "completed") {
      el("session-status").textContent = "Session complete · Analysis errors";
      el("processing-title").textContent = "Analysis incomplete";
    }
    el("inference-errors").hidden = !failures.length;
    if (failures.length) {
      const code = failures.at(-1).error?.status_code;
      const issues=[...new Set(failures.flatMap(item=>Object.values(item.event_errors || {})))];
      const detail = issues.length ? issues.join(" ")+" Successful checks are retained." : ({401:"Check your local API key.",403:"Check API access.",402:"Check Meta billing or credits.",429:"Meta rate limit reached."})[code] || "Failed windows add no detected activity.";
      el("inference-errors").textContent = `${failures.length} ${failures.length === 1 ? "window has" : "windows have"} incomplete checks. ${detail}`;
    }
    const skipped = state.skipped_windows;
    el("skipped-warning").hidden = !skipped.length;
    if (skipped.length) el("skipped-warning").innerHTML = `<details><summary>${skipped.length} analysis ${skipped.length === 1 ? "window was" : "windows were"} skipped</summary><p>Windows are skipped when the queue is full or no monitoring is enabled. Unprocessed time adds no detected activity.</p><p>${skipped.map(job => `${clock(job.start_seconds)}–${clock(job.end_seconds)}${job.skip_reason ? ` · ${escapeHTML(job.skip_reason)}` : " · Queue full"}`).join("<br>")}</p></details>`;
    el("live-events").innerHTML = cards(state.events, true);
    el("live-event-count").textContent = state.events.length;
    el("alert-count").textContent = state.alerts.length;
    renderAlertFeed();
    state.alerts.forEach(alert => { if (!seen.has(alert.id)) { seen.add(alert.id); toast(alert); } });
    renderUnread();
    review.update(reviewState(),sessionSource,read);
    if (state.status === "live") {
      if (!webcam && Math.abs(liveVideo.currentTime - state.captured_seconds) > 1) liveVideo.currentTime = state.captured_seconds;
    } else {
      if (webcam) { camera.stop(); source = null; }
      liveVideo.pause();
      el("resume-video").hidden = true;
    }
  }

  async function poll() {
    if (!state || polling || !["live","finishing"].includes(state.status) && !state.evidence_pending) return;
    polling = true;
    const requestedSession = state.id;
    try {
      const next = await api(`/api/monitor/${requestedSession}`);
      if (state.id === requestedSession) acceptState(next);
      error("");
    }
    catch (failure) {
      if (failure.status === 404) {
        state.status = "stopped";
        state.active_window = null;
        state.queue_size = 0;
        renderLive();
      }
      error(`${failure.message} Analysis status is unavailable.`);
    }
    finally { polling = false; }
  }

  el("go-live").addEventListener("click", async () => {
    if (starting || openingCamera || !source?.duration) return;
    const selectedSource = {...source};
    const settings = {duration:source.duration, source_name:source.name, events:drafts.map(event => ({...event})), provider,
      window_seconds:Number(el("window").value), overlap_seconds:Number(el("overlap").value),
      merge_gap_seconds:Number(el("merge-gap").value),
      safety_watch:el("safety-setup").checked,
      sam_snapshots:false,
      safety_prompt:safetyPrompt,safety_exclusions:safetyExclusions,
      model:el("model").value,video_reasoning_effort:el("video-reasoning").value,
      audio_model:el("audio-model").value,audio_reasoning_effort:el("audio-reasoning").value,
      capture_audio:source.kind === "webcam" && !!source.capture_audio};
    if (source.kind === "webcam") settings.source_type = "webcam";
    if (provider === "mock") settings.mock_delay_seconds = Number(el("mock-delay").value);
    if (!Number.isFinite(settings.window_seconds) || settings.window_seconds < 1 || settings.window_seconds > 120 ||
        !Number.isFinite(settings.overlap_seconds) || settings.overlap_seconds < 0 || settings.window_seconds-settings.overlap_seconds < 1 ||
        !Number.isFinite(settings.merge_gap_seconds) || settings.merge_gap_seconds < 0 || settings.merge_gap_seconds > 30 ||
        provider === "mock" && (!Number.isFinite(settings.mock_delay_seconds) || settings.mock_delay_seconds < 0.1 || settings.mock_delay_seconds > 300)) {
      error("Use a 1–120s window, at least a 1s stride, a 0–30s event gap tolerance, and a 0.1–300s mock processing time.");
      return;
    }
    starting = true;
    renderDrafts();
    try {
      if (provider === "muse" && selectedSource.kind !== "webcam") {
        if (source.file) {
          if (source.file.size > 200 * 1024 * 1024) throw new Error("Choose a video up to 200 MB for Muse mode.");
          el("go-live-note").textContent = "Copying video to the local backend…";
          const response = await fetch("/api/monitor/source", {method:"POST",headers:{"Content-Type":"application/octet-stream"},body:source.file});
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "Could not load the video.");
          settings.source_id = result.source_id;
        } else settings.source_id = "sample";
      }
      if (selectedSource.kind === "webcam") {
        liveVideo.removeAttribute("src");
        liveVideo.srcObject = selectedSource.stream;
      } else {
        liveVideo.srcObject = null;
        liveVideo.src = selectedSource.url;
      }
      await liveVideo.play();
      state = await api("/api/monitor", settings);
      sessionSource = selectedSource;
      if (selectedSource.kind === "webcam") camera.start(state.id);
      seen.clear(); read.clear(); el("toasts").replaceChildren();
      page("live"); renderLive(); error("");
    } catch (failure) {
      liveVideo.pause();
      if (selectedSource.kind === "webcam") {
        camera.stop(); source = null;
        if (state?.source_type === "webcam" && state.status === "live") {
          try { acceptState(await api(`/api/monitor/${state.id}/stop`,{})); } catch {}
        }
      }
      error(failure.message);
    }
    finally { starting = false; setMode(provider); renderDrafts(); }
  });
  el("stop").addEventListener("click", async () => {
    el("stop").disabled = true;
    if (state.source_type === "webcam") camera.stop();
    try { acceptState(await api(`/api/monitor/${state.id}/stop`, {})); error(""); }
    catch (failure) { error(failure.message); }
    finally { el("stop").disabled = false; }
  });
  el("setup-nav").addEventListener("click", () => page("setup"));
  el("live-nav").addEventListener("click", () => page("live"));
  el("back-setup").addEventListener("click", () => { page("setup"); setMode(provider); });
  liveVideo.addEventListener("contextmenu", event => event.preventDefault());
  liveVideo.addEventListener("pause", async () => {
    if (state?.status !== "live" || liveVideo.ended) return;
    try { await liveVideo.play(); } catch { el("resume-video").hidden = false; }
  });
  liveVideo.addEventListener("error", () => { if (state?.status === "live") error("The live video stopped loading. Stop monitoring and choose another file."); });
  el("resume-video").addEventListener("click", async () => {
    try { if (state.source_type !== "webcam") liveVideo.currentTime = state.captured_seconds; await liveVideo.play(); el("resume-video").hidden = true; }
    catch { error("Video playback could not resume. Stop monitoring and try another file."); }
  });
  el("model").addEventListener("change",()=>{renderReasoning();setMode(provider);});
  el("video-reasoning").addEventListener("change",()=>setMode(provider));
  el("audio-model").addEventListener("change",()=>{renderAudioReasoning();renderAudioSettings();savePreferences();});
  el("capture-audio").addEventListener("change",()=>{renderSourceMode();savePreferences();});
  for (const id of ["window","overlap","merge-gap","model","video-reasoning","audio-model","audio-reasoning","suggestion-reasoning","capture-audio","mock-delay"]) {
    el(id).addEventListener("input",savePreferences);
    el(id).addEventListener("change",savePreferences);
  }
  window.addEventListener("beforeunload", () => { camera.stop(); savePreferences(); objectURLs.forEach(url => URL.revokeObjectURL(url)); });
  restorePreferences();
  renderDrafts();
  api("/api/monitor").then(config => {
    safetyDefaults = config.safety_defaults;
    el("edit-safety-setup").disabled = !safetyDefaults;
    sampleURL = config.sample_url;
    if (!savedSetup && !changedSinceLoad && config.session?.events.length) {
      const session = config.session;
      const recovered = SetupPreferences.normalize({version:1,events:session.events,provider:session.provider,
        window_seconds:session.window_seconds,overlap_seconds:session.overlap_seconds,model:session.model,
        video_reasoning_effort:session.video_reasoning_effort,
        audio_model:session.audio_model,audio_reasoning_effort:session.audio_reasoning_effort,capture_audio:session.capture_audio,
        merge_gap_seconds:session.merge_gap_seconds,
        safety_watch:session.safety_watch,
        sam_snapshots:session.sam_snapshots,
        safety_prompt:session.safety_prompt,safety_exclusions:session.safety_exclusions,
        mock_delay_seconds:session.mock_delay_seconds,
        source:session.source_type === "webcam" ? {kind:"webcam"} : session.source_name === "Kitten stuck · Sample" ? {kind:"sample"} : {kind:"file",name:session.source_name}});
      if (recovered) {
        preferences = recovered;
        safetyPrompt = preferences.safety_prompt;
        safetyExclusions = preferences.safety_exclusions;
        drafts.splice(0,drafts.length,...preferences.events);
        removed.splice(0,removed.length,...preferences.removed_events);
        sourcePreference = preferences.source;
        restorePreferences();
        renderDrafts();
        savePreferences();
      }
    }
    const selectedModel = el("model").value;
    el("model").innerHTML = Object.entries(config.models).map(([id,name]) => `<option value="${escapeHTML(id)}">${escapeHTML(name)}</option>`).join("");
    if (Object.hasOwn(config.models,selectedModel)) el("model").value = selectedModel;
    el("sample").disabled = !sampleURL;
    if (sourcePreference.kind === "file") {
      if (!source?.file) {
        el("source-name").textContent = sourcePreference.name;
        el("source-detail").textContent = "Select this local video again after refreshing.";
      }
    } else if (sourcePreference.kind === "sample" && sampleURL && !source) selectSource(sampleURL, "Kitten stuck · Sample",null,false);
    if (savedSetup && !changedSinceLoad) el("setup-save-status").textContent = "Events and settings restored from this browser.";
    if (config.session && ["live","finishing"].includes(config.session.status)) {
      previousSession = config.session;
      el("previous-session-message").textContent = `A previous ${config.session.provider === "muse" ? "Muse" : "mock"} session is still running.`;
      el("previous-session").hidden = false;
      renderDrafts();
      el("stop-previous").addEventListener("click", async () => {
        try {
          await api(`/api/monitor/${config.session.id}/stop`, {});
          previousSession = null;
          el("previous-session").hidden = true;
          renderDrafts();
          error("");
        } catch (failure) { error(failure.message); }
      });
    }
  }).catch(failure => error(failure.message));
  setInterval(poll, 500);
})();
