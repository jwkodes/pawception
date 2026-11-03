/* Independent review player. Frame extraction stays local to the browser. */
(() => {
  "use strict";
  const escapeHTML = value => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const clock = value => `${Math.floor(value / 60).toString().padStart(2,"0")}:${Math.floor(value % 60).toString().padStart(2,"0")}`;

  function capturedTime(time, captured, duration) {
    const maximum = Math.max(0, Math.min(captured, duration));
    return Math.max(0, Math.min(Number.isFinite(time) ? time : 0, maximum));
  }

  function visibleActivities(activities, captured, duration) {
    return activities.map(activity => ({...activity,
      start:capturedTime(activity.start, captured, duration),
      end:capturedTime(activity.end, captured, duration)
    })).filter(activity => activity.end > activity.start);
  }

  function matchingAlert(activity, alerts) {
    return alerts.find(alert => alert.event_id === activity.event_id &&
      alert.at >= activity.start && alert.at <= activity.end);
  }

  function eventColors(event, fallback = 0) {
    const numbered = /^event_(\d+)$/.exec(event?.id || "");
    const index = Number.isInteger(event?.palette_index) ? event.palette_index : numbered ? Number(numbered[1])-1 : fallback;
    const hue = ((index * 137.508 + 110) % 360).toFixed(1);
    return {solid:`hsl(${hue} 38% 49%)`, soft:`hsl(${hue} 35% 94%)`, ink:`hsl(${hue} 40% 31%)`};
  }

  function eventStyle(event, fallback = 0) {
    const color = eventColors(event,fallback);
    return `--event-color:${color.solid};--event-soft:${color.soft};--event-ink:${color.ink};`;
  }

  function processingLanes(windows) {
    const lanes = [];
    for (const job of windows) {
      let lane = lanes.find(items => items.at(-1).end_seconds <= job.start_seconds + 0.000001);
      if (!lane) lanes.push(lane=[]);
      lane.push(job);
    }
    return lanes;
  }

  function safetyTimelineStatus(state) {
    if (!state.safety_watch) return "Safety watch off";
    const checked = state.windows.filter(job=>job.safety?.enabled && (job.status==="completed" || job.safety_checked)).length;
    const count = state.hazards?.length || 0;
    if (count) return `${count} ${count===1 ? "hazard" : "hazards"} detected`;
    if (checked) return `No hazards detected · ${checked} ${checked===1 ? "window" : "windows"} checked`;
    if (state.windows.some(job=>job.safety?.enabled && ["error","invalid"].includes(job.status))) return "No valid safety results";
    return "Waiting for first safety result";
  }

  const windowStatus = {completed:"Processed",partial:"Partly checked",running:"Processing",pending:"Queued",error:"Failed",invalid:"Failed",skipped:"Skipped",cancelled:"Cancelled"};

  class FrameSampler {
    constructor() {
      this.video = document.createElement("video");
      this.video.muted = true;
      this.video.playsInline = true;
      this.video.preload = "auto";
      this.cache = new Map();
      this.queue = [];
      this.busy = false;
    }

    request(time, callback, hover = false) {
      const key = Math.floor(time * 4) / 4;
      if (this.cache.has(key)) { callback(this.cache.get(key)); return; }
      // Keep only the latest unstarted hover, rather than decoding every mouse move.
      if (hover) this.queue = this.queue.filter(job => !job.hover);
      const job = {time:key, callback, hover};
      if (hover) this.queue.unshift(job); else this.queue.push(job);
      this.pump();
    }

    waitFor(event, action) {
      return new Promise((resolve, reject) => {
        const clean = () => {
          clearTimeout(timer);
          this.video.removeEventListener(event, done);
          this.video.removeEventListener("error", failed);
        };
        const done = () => { clean(); resolve(); };
        const failed = () => { clean(); reject(new Error("Frame unavailable")); };
        const timer = setTimeout(failed, 8000);
        this.video.addEventListener(event, done, {once:true});
        this.video.addEventListener("error", failed, {once:true});
        try { action?.(); } catch { failed(); }
      });
    }

    async pump() {
      if (this.busy) return;
      this.busy = true;
      while (this.queue.length) {
        const job = this.queue.shift();
        try {
          if (this.cameraId) {
            const time = Math.max(0,Math.min(job.time,this.captured-0.1));
            const frame = cameraMediaURL(this.cameraId,time,Math.min(this.captured,time+0.1),true);
            this.cache.set(job.time,frame);
            if (this.cache.size>80) this.cache.delete(this.cache.keys().next().value);
            job.callback(frame);
            continue;
          }
          if (this.video.readyState < 1) await this.waitFor("loadedmetadata");
          if (Math.abs(this.video.currentTime - job.time) > 0.01) {
            await this.waitFor("seeked", () => { this.video.currentTime = job.time; });
          }
          if (this.video.readyState < 2) await this.waitFor("loadeddata");
          const canvas = document.createElement("canvas");
          canvas.width = 288;
          canvas.height = Math.round(288 * this.video.videoHeight / this.video.videoWidth);
          canvas.getContext("2d").drawImage(this.video, 0, 0, canvas.width, canvas.height);
          const frame = canvas.toDataURL("image/jpeg", 0.65);
          this.cache.set(job.time, frame);
          if (this.cache.size > 80) this.cache.delete(this.cache.keys().next().value);
          job.callback(frame);
        } catch { job.callback(null); }
      }
      this.busy = false;
    }

    destroy() {
      this.queue = [];
      this.video.removeAttribute("src");
      this.video.load();
    }
  }

  class ActivityReview {
    constructor() {
      this.root = document.getElementById("activity-review");
      this.timeline = document.getElementById("review-timeline");
      this.video = document.getElementById("review-video");
      const sound = document.getElementById('review-sound');
      if (sound) {
        const updateSound = () => {
          const muted = this.video.muted;
          sound.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4Z"/>${muted ? '<path d="m16 9 5 6m0-6-5 6"/>' : '<path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>'}</svg>`;
          sound.classList.toggle('is-muted',muted);
          sound.setAttribute('aria-label',muted ? 'Unmute review audio' : 'Mute review audio');
          sound.setAttribute('aria-pressed',String(!muted));
          sound.title = muted ? 'Sound muted · Click to unmute' : 'Sound on · Click to mute';
        };
        sound.addEventListener('click',()=>{ this.video.muted = !this.video.muted; });
        this.video.addEventListener('volumechange',updateSound);
        updateSound();
      }
      this.scrubber = document.getElementById("review-scrubber");
      this.playButton = document.getElementById("review-play");
      this.popup = document.createElement("div");
      this.popup.className = "review-popover";
      this.popup.id = "review-hover-preview";
      this.popup.setAttribute("role", "tooltip");
      this.popup.hidden = true;
      document.body.append(this.popup);
      this.target = 0;
      this.offset = 0;
      this.hoverToken = 0;
      this.video.addEventListener("loadedmetadata", () => { this.video.currentTime = Math.max(0,this.target-this.offset); });
      this.video.addEventListener("ended",()=>{
        const next = this.offset+this.video.duration;
        if (this.state?.source_type === "webcam" && next<this.state.captured_seconds-0.1) this.seek(next,true);
      });
      this.video.addEventListener("timeupdate", () => this.updatePosition());
      this.video.addEventListener("play", () => this.updatePosition());
      this.video.addEventListener("pause", () => this.updatePosition());
      this.playButton.addEventListener("click", () => {
        if (this.video.paused) this.seek(this.video.currentTime+this.offset, true); else this.video.pause();
      });
      this.scrubber.addEventListener("input", () => this.seek(Number(this.scrubber.value), true));
      this.timeline.addEventListener("pointermove", event => this.hover(event));
      this.timeline.addEventListener("pointerleave", () => this.hideHover());
      this.timeline.addEventListener("focusin", event => {
        if (event.target.matches(":focus-visible")) this.hover(event, true);
      });
      this.timeline.addEventListener("focusout", () => this.hideHover());
      this.timeline.addEventListener("click", event => this.click(event));
      this.timeline.addEventListener("keydown", event => {
        if (event.key === "Escape") this.hideHover();
        if (["Enter", " "].includes(event.key) && event.target.matches("[data-track][role=button]")) {
          event.preventDefault();
          this.click({target:event.target,detail:0});
        }
      });
      window.addEventListener("scroll", () => this.hideHover(), {passive:true});
      document.getElementById("notifications")?.addEventListener("click", () => this.hideHover());
    }

    update(state, source, viewed) {
      if (this.sessionId !== state.id) {
        this.sessionId = state.id;
        this.sampler?.destroy();
        this.sampler = new FrameSampler();
        if (state.source_type === "webcam") {
          this.sampler.cameraId = state.id;
          this.video.removeAttribute("src"); this.video.load();
        } else {
          this.sampler.video.src = source.url;
          this.video.src = source.url;
        }
        this.video.pause();
        this.target = 0;
        this.offset = 0;
        this.clipEnd = 0;
        this.signature = null;
        this.selected = null;
        this.buildTimeline(state.duration);
        this.timelineDuration = state.duration;
      }
      this.state = state;
      this.sampler.captured = state.captured_seconds;
      if (this.timelineDuration !== state.duration) {
        this.buildTimeline(state.duration);
        this.timelineDuration = state.duration;
        this.signature = null;
      }
      this.root.querySelector(".review-context .field-help").textContent = state.offline
        ? state.mixed_providers ? "Rows marked Mock are synthetic. Other rows show Muse detections. Click a coloured interval to review footage."
          : state.provider === "muse" ? "Search results are model detections. Click a coloured interval to review the footage." : "Mock search results are synthetic, not observations of this recording."
        : state.provider === "muse"
        ? "Highlighted activities are Muse detections. Opening review footage does not mark an alert as viewed."
        : "Highlighted activities are scripted mock observations. Opening review footage does not mark an alert as viewed.";
      this.viewed = viewed;
      this.activities = visibleActivities(state.detections, state.captured_seconds, state.duration);
      this.scrubber.max = state.captured_seconds;
      this.scrubber.disabled = this.playButton.disabled = state.captured_seconds < 0.2;
      document.getElementById("review-available").textContent = `${clock(state.captured_seconds)} ${state.offline ? 'recorded' : 'captured'}`;
      const percent = 100 * state.captured_seconds / state.duration;
      this.timeline.querySelectorAll(".review-future").forEach(mask => { mask.style.left = `${percent}%`; });
      this.timeline.querySelector(".review-captured-line").style.left = `${percent}%`;
      for (const image of this.timeline.querySelectorAll(".review-filmstrip img")) {
        const time = Number(image.dataset.time);
        if (time > state.captured_seconds || image.dataset.requested) continue;
        image.dataset.requested = "true";
        this.sampler.request(time, frame => {
          if (frame && image.isConnected) { image.src = frame; image.hidden = false; }
        });
      }
      const signature = JSON.stringify([this.activities, state.alerts, [...viewed],state.safety_watch,safetyTimelineStatus(state),
        state.events.map(event=>[event.id,event.name,event.palette_index]),
        state.windows.map(job=>[job.id,job.status,job.start_seconds,job.end_seconds])]);
      if (signature !== this.signature) {
        this.signature = signature;
        this.drawActivities();
      }
      this.updatePosition();
    }

    buildTimeline(duration) {
      const axis = [0,1,2,3,4].map(index => `<span style="left:${index*25}%">${clock(duration*index/4)}</span>`).join("");
      const frames = Array.from({length:12}, (_,index) => `<span class="review-film-cell"><img alt="" hidden data-time="${duration*(index+0.5)/12}"></span>`).join("");
      this.timeline.innerHTML = `<div class="review-axis-row"><span>Source time</span><div>${axis}</div></div>
        <div class="review-track-row"><span>Footage</span><div class="review-track review-filmstrip" data-track tabindex="0" role="button" aria-label="Footage filmstrip. Click to review captured footage.">${frames}<div class="review-future"></div></div></div>
        <div id="review-activity-tracks"></div>
        <div class="review-cursor-layer"><i class="review-captured-line" title="Captured through"></i><i class="review-position-line"></i></div>`;
    }

    bar(start, end, css, attributes, colorStyle = "") {
      return `<button class="review-band ${css}" style="${colorStyle}left:${100*start/this.state.duration}%;width:${100*(end-start)/this.state.duration}%" ${attributes}></button>`;
    }

    drawActivities() {
      this.hideHover();
      let html = "";
      const lanes = processingLanes(this.state.windows);
      if (!lanes.length) lanes.push([]);
      lanes.forEach((lane,index) => {
        const windows = lane.map(job=>this.bar(job.start_seconds,job.end_seconds,`review-window ${job.status}`,
          `data-window="${job.id}" aria-describedby="review-hover-preview" aria-label="Window ${job.id}, ${clock(job.start_seconds)} to ${clock(job.end_seconds)}, ${windowStatus[job.status] || job.status}"`)).join("");
        html += `<div class="review-track-row"><span>${index===0 ? "Processing" : `Processing ${index+1}`}</span><div class="review-track" data-track data-processing>${windows}<div class="review-future"></div></div></div>`;
      });
      const danger = this.activities.map((activity,index)=>activity.source==="safety"
        ? this.bar(activity.start,activity.end,"review-safety-band",
          `data-activity="${index}" aria-describedby="review-hover-preview" aria-label="Safety hazard, ${escapeHTML(this.state.events.find(event=>event.id===activity.event_id)?.name || "Potential danger")}, ${clock(activity.start)} to ${clock(activity.end)}. Play evidence."`)
        : "").join("");
      const safetyStatus = safetyTimelineStatus(this.state);
      if (!this.state.offline) html += `<div class="review-track-row review-safety-row ${this.state.safety_watch ? "" : "is-off"}">
        <span title="${escapeHTML(safetyStatus)}">Danger</span>
        <div class="review-track" data-track data-safety-track ${danger ? 'role="group"' : 'tabindex="0" role="button"'} aria-label="Safety watch danger overview. ${escapeHTML(safetyStatus)}. Click to review footage.">
          ${danger}${danger ? "" : `<span class="review-safety-empty">${escapeHTML(safetyStatus)}</span>`}<div class="review-future"></div>
        </div></div>`;
      this.state.events.forEach((event,index) => {
        const bands = this.activities.map((activity,i) => {
          if (activity.event_id !== event.id) return "";
          return this.bar(activity.start, activity.end, "activity",
            `data-activity="${i}" aria-describedby="review-hover-preview" aria-label="${escapeHTML(event.name)}, ${clock(activity.start)} to ${clock(activity.end)}. Play activity."`);
        }).join("");
        const markers = this.state.alerts.filter(alert=>alert.event_id===event.id).map(alert => this.bar(alert.at, alert.at,
          `review-alert ${this.viewed.has(alert.id) ? "is-viewed" : "is-unread"}`,
          `data-alert="${alert.id}" aria-label="${escapeHTML(alert.event_name)}, ${clock(alert.at)}, ${this.viewed.has(alert.id) ? "viewed" : "unread"} alert"`,eventStyle(event))).join("");
        html += `<div class="review-track-row" style="${eventStyle(event,index)}"><span title="${escapeHTML(event.name)}"><i class="event-type-dot"></i>${escapeHTML(event.name)}</span><div class="review-track" data-track>${bands}${markers}<div class="review-future"></div></div></div>`;
      });
      if (this.state.skipped_windows.length) {
        html += `<div class="review-track-row"><span>Skipped analysis</span><div class="review-track" data-track>${this.state.skipped_windows.map(job => this.bar(job.start_seconds,job.end_seconds,"review-skipped",`data-skipped="true" aria-label="Skipped analysis window, ${clock(job.start_seconds)} to ${clock(job.end_seconds)}"`)).join("")}</div></div>`;
      }
      this.timeline.querySelector("#review-activity-tracks").innerHTML = html;
      const percent = 100 * this.state.captured_seconds / this.state.duration;
      this.timeline.querySelectorAll(".review-future").forEach(mask => { mask.style.left = `${percent}%`; });
      if (this.selected) this.showContext(this.selected);
    }

    point(event, keyboard = false) {
      const track = event.target.closest("[data-track]");
      if (!track) return null;
      const bounds = track.getBoundingClientRect();
      const button = event.target.closest(".review-band");
      const buttonBounds = button?.getBoundingClientRect();
      const x = keyboard ? (buttonBounds ? buttonBounds.left+buttonBounds.width/2 : bounds.left) : event.clientX;
      const time = Math.max(0, Math.min(this.state.duration, (x-bounds.left)/bounds.width*this.state.duration));
      return {time, x, y:keyboard ? bounds.top : event.clientY};
    }

    hover(event, keyboard = false) {
      const point = this.point(event,keyboard);
      if (!point) { this.hideHover(); return; }
      clearTimeout(this.hoverTimer);
      const token = ++this.hoverToken;
      const available = point.time <= this.state.captured_seconds;
      const activity = this.activities[Number(event.target.closest("[data-activity]")?.dataset.activity)];
      const alertId = event.target.closest("[data-alert]")?.dataset.alert;
      const alert = alertId ? this.state.alerts.find(item => item.id === alertId) : activity && matchingAlert(activity,this.state.alerts);
      const definition = this.state.events.find(item => item.id === activity?.event_id);
      const jobId = event.target.closest("[data-window]")?.dataset.window;
      const job = jobId && this.state.windows.find(item=>item.id===jobId);
      let title = available ? `Frame at ${clock(point.time)}` : "Not captured yet";
      let detail = available ? "Click to play from this point." : "Review becomes available as the live replay progresses.";
      if (job) {
        title = `Window ${job.id} · ${windowStatus[job.status] || job.status}`;
        detail = `${clock(job.start_seconds)}–${clock(job.end_seconds)}`;
        for (const issue of Object.values(job.event_errors || {})) detail += ` · ${issue}`;
        for (const check of job.checks || []) detail += ` · ${check.model}: ${check.media_kind} (${check.status})`;
        const failure = this.state.failed_windows?.find(item=>item.id===job.id)?.error;
        if (failure) detail += ` · ${failure.type || "API error"}${failure.status_code ? ` (${failure.status_code})` : ""}`;
        const explanations = {partial:"Some input checks were unavailable. Successful detections are retained.",completed:"Analysis completed. This includes windows where no event was detected.",running:"Checks run in parallel. Completed results are already shown; remaining checks are still processing.",pending:"Captured and waiting for the inference worker.",error:"Processing failed. This is not a negative event detection.",invalid:"No valid observation was returned.",skipped:"This window was skipped because the queue was full.",cancelled:"Processing was cancelled when the session stopped."};
        detail += ` · ${job.skip_reason || explanations[job.status] || ""}`;
      } else if (activity) {
        title = definition.name;
        const detected = activity.detected_seconds ?? activity.end-activity.start;
        const gap = activity.end-activity.start-detected;
        detail = `${clock(activity.start)}–${clock(activity.end)} · ${detected.toFixed(1)}s detected${gap>0.001 ? ` · ${gap.toFixed(1)}s bridged gaps` : ""}`;
      } else if (alert) {
        title = alert.event_name;
        detail = `${clock(alert.at)} · ${alert.source === "safety" ? "Safety watch · First detection" : `${alert.threshold_seconds}s duration threshold`}`;
      } else if (event.target.closest("[data-skipped]")) {
        title = "Analysis window skipped";
        detail = "Footage is available for review. This window was not analyzed.";
      }
      this.popup.innerHTML = `<img alt="Frame preview" hidden><div><strong>${escapeHTML(title)}</strong><span>${escapeHTML(detail)}</span>${definition ? `<p>${escapeHTML(definition.description)}</p>` : ""}${alert ? `<small class="${this.viewed.has(alert.id) ? "viewed-label" : "unread-label"}">${this.viewed.has(alert.id) ? "✓ Viewed alert" : "● Unread alert"}</small>` : ""}</div>`;
      const colorEvent = definition || this.state.events.find(event=>event.id===alert?.event_id);
      this.popup.style.borderColor = colorEvent ? eventColors(colorEvent).solid : "#d3ddc7";
      this.popup.querySelector("strong").style.color = colorEvent ? eventColors(colorEvent).ink : "";
      this.popup.hidden = false;
      this.popup.style.left = `${Math.max(10,Math.min(window.innerWidth-310,point.x-145))}px`;
      this.popup.style.top = `${Math.max(10,point.y-this.popup.offsetHeight-18)}px`;
      if (available) this.hoverTimer = setTimeout(() => {
        this.sampler.request(capturedTime(point.time,this.state.captured_seconds,this.state.duration), frame => {
          if (token !== this.hoverToken || !frame) return;
          const image = this.popup.querySelector("img");
          image.src = frame;
          image.hidden = false;
          this.popup.style.top = `${Math.max(10,point.y-this.popup.offsetHeight-18)}px`;
        }, true);
      }, 70);
    }

    hideHover() {
      clearTimeout(this.hoverTimer);
      this.hoverToken++;
      this.popup.hidden = true;
    }

    click(event) {
      if (event.target.closest("[data-alert]")) { this.hideHover(); return; }
      const point = this.point(event, event.detail === 0);
      if (!point || point.time > this.state.captured_seconds) return;
      const activity = this.activities[Number(event.target.closest("[data-activity]")?.dataset.activity)];
      this.selected = activity || this.activities.find(item => point.time >= item.start && point.time <= item.end);
      this.showContext(this.selected);
      this.seek(event.detail === 0 && activity ? activity.start : point.time, true);
      this.hideHover();
    }

    showContext(activity) {
      const definition = this.state.events.find(event => event.id === activity?.event_id);
      const alert = activity && matchingAlert(activity,this.state.alerts);
      this.root.querySelector(".review-context").style.cssText = definition ? eventStyle(definition) : "";
      document.getElementById("review-context-title").textContent = definition?.name || "A little more context.";
      document.getElementById("review-context-copy").textContent = activity
        ? `${clock(activity.start)}–${clock(activity.end)} · ${definition.description} Review before and after using the filmstrip below.`
        : this.state.offline ? "Review this saved recording at any point. Search results stay on the timeline."
          : "Review captured footage at any point. The live feed continues independently.";
      document.getElementById("review-context-event").innerHTML = alert
        ? `<button class="review-open-alert" data-alert="${alert.id}">${this.viewed.has(alert.id) ? "✓ Viewed" : "● Unread"} · Open alert ↗</button>` : "";
    }

    seek(time, play = false) {
      if (!this.state) return;
      this.target = capturedTime(time,this.state.captured_seconds,this.state.duration);
      if (this.state.source_type === "webcam") {
        this.target = Math.min(this.target,Math.max(0,this.state.captured_seconds-0.1));
        if (!this.video.getAttribute("src") || this.target<this.offset || this.target>=this.clipEnd-0.1) {
          this.offset = Math.max(0,this.target-2);
          this.clipEnd = Math.min(this.state.captured_seconds,this.offset+30);
          this.video.src = cameraMediaURL(this.state.id,this.offset,this.clipEnd);
        } else if (this.video.readyState>=1) this.video.currentTime = this.target-this.offset;
      } else if (this.video.readyState >= 1) this.video.currentTime = this.target;
      if (play) this.video.play().catch(() => {});
      this.updatePosition();
    }

    updatePosition() {
      if (!this.state) return;
      const maximum = Math.min(this.state.captured_seconds, this.state.duration);
      if (this.video.currentTime+this.offset >= maximum && !this.video.paused) this.video.pause();
      if (this.video.currentTime+this.offset > maximum + 0.05) this.video.currentTime = Math.max(0,maximum-this.offset);
      const time = capturedTime(this.video.currentTime+this.offset,maximum,this.state.duration);
      this.scrubber.value = time;
      document.getElementById("review-position").textContent = clock(time);
      document.getElementById("review-video-time").textContent = clock(time);
      this.playButton.textContent = this.video.paused ? "▶" : "Ⅱ";
      this.playButton.setAttribute("aria-label", this.video.paused ? "Play review" : "Pause review");
      const line = this.timeline.querySelector(".review-position-line");
      if (line) line.style.left = `${100*time/this.state.duration}%`;
    }
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {capturedTime,visibleActivities,matchingAlert,eventColors,eventStyle,processingLanes,safetyTimelineStatus};
  else { window.ActivityReview = ActivityReview; window.eventStyle = eventStyle; window.safetyTimelineStatus = safetyTimelineStatus; }
})();
