/* One recorder, ordered uploads, and a bounded transport queue. */
(() => {
  "use strict";
  class CameraCapture {
    constructor({onState, onError, Recorder = globalThis.MediaRecorder,
                 mediaDevices = globalThis.navigator?.mediaDevices,
                 clock = () => performance.now(), send = (url,options) => globalThis.fetch(url,options)} = {}) {
      Object.assign(this,{onState,onError,Recorder,mediaDevices,clock,send});
      this.queue = [];
      this.active = false;
      this.uploading = false;
      this.generation = 0;
    }

    async open({audio=false}={}) {
      if (!this.mediaDevices?.getUserMedia || !this.Recorder) {
        throw new Error("Camera recording is unavailable here. Open this localhost page in Chrome, Edge, or Safari.");
      }
      this.stop();
      try {
        this.stream = await this.mediaDevices.getUserMedia({audio,
          video:{width:{ideal:854,max:854},height:{ideal:480,max:480},frameRate:{ideal:15,max:15}}});
      } catch (failure) {
        const devices = audio ? "camera and microphone" : "camera";
        const messages = {NotAllowedError:`Access was denied. Allow ${devices} access for this page and try again.`,
          NotFoundError:`The requested ${devices} could not be found. Connect them and try again.`,
          NotReadableError:`The ${devices} could not open. Close other apps using them and try again.`};
        throw new Error(messages[failure.name] || "The camera could not open. Check camera access and try again.");
      }
      const stream = this.stream;
      stream.getVideoTracks().forEach(track=>track.addEventListener("ended",()=>{
        if (this.stream !== stream) return;
        if (this.active) this.fail("The camera disconnected. Monitoring has stopped.");
        else { this.stop(); this.onError?.("The camera disconnected. Reconnect it to continue."); }
      }));
      return this.stream;
    }

    start(sessionId) {
      const mimeType = [...(this.stream.getAudioTracks?.().length ? ["video/webm;codecs=vp8,opus"] : []),"video/webm;codecs=vp8","video/webm","video/mp4"]
        .find(type=>this.Recorder.isTypeSupported(type));
      this.recorder = new this.Recorder(this.stream,{...(mimeType ? {mimeType} : {}),videoBitsPerSecond:800000});
      this.controller = new AbortController();
      this.sessionId = sessionId;
      this.sequence = 0;
      this.queue = [];
      this.started = this.clock();
      this.active = true;
      const generation = this.generation;
      const current = () => this.active && this.generation === generation;
      this.recorder.addEventListener("dataavailable",event=>{ if (current()) this.accept(event.data); });
      this.recorder.addEventListener("error",()=>{
        if (current()) this.fail("Camera recording failed. Monitoring has stopped.");
      });
      this.recorder.addEventListener("stop",()=>{
        if (current()) this.fail("Camera recording ended unexpectedly. Monitoring has stopped.");
      });
      this.recorder.start(1000);
    }

    accept(blob) {
      if (!this.active || !blob.size) return;
      // Recording chunks depend on earlier bytes. Never silently drop a chunk.
      if (this.queue.length>=2 || blob.size>8*1024*1024) {
        this.fail("Camera uploads cannot keep up. Monitoring stopped to avoid an incomplete recording. Try a shorter session or a faster local connection.");
        return;
      }
      this.queue.push({blob,sequence:this.sequence++,end:(this.clock()-this.started)/1000});
      this.pump();
    }

    async pump() {
      if (this.uploading || !this.active) return;
      this.uploading = true;
      const sessionId = this.sessionId;
      const generation = this.generation;
      try {
        while (this.active && this.queue.length && this.generation===generation) {
          const packet = this.queue.shift();
          const timeout = this.uploadTimeout = setTimeout(()=>this.fail("Camera upload timed out. Monitoring has stopped."),10000);
          try {
            const response = await this.send(`/api/monitor/${sessionId}/camera`,{method:"POST",
              headers:{"Content-Type":this.recorder.mimeType.split(";")[0],
                "X-Capture-Sequence":String(packet.sequence),"X-Capture-End":String(packet.end)},
              body:packet.blob,signal:this.controller.signal});
            const state = await response.json();
            if (!response.ok) throw new Error(state.error || "Camera upload failed.");
            if (this.active && this.generation===generation) this.onState?.(state);
          } finally { clearTimeout(timeout); }
        }
      } catch (failure) {
        if (this.active && this.generation===generation) this.fail(failure.message);
      } finally { if (this.generation===generation) this.uploading = false; }
    }

    fail(message) {
      if (!this.active) return;
      this.stop();
      this.onError?.(message);
    }

    stop() {
      this.active = false;
      this.generation++;
      this.uploading = false;
      this.queue = [];
      this.controller?.abort();
      clearTimeout(this.uploadTimeout);
      if (this.recorder?.state === "recording") this.recorder.stop();
      this.stream?.getTracks().forEach(track=>track.stop());
      this.stream = null;
    }
  }

  function cameraMediaURL(id, start, end, frame = false) {
    start = Math.floor(start*1000)/1000;
    end = Math.floor(end*1000)/1000;
    return `/webcam-media/${encodeURIComponent(id)}/${frame ? "frame.jpg" : "clip.mp4"}?start=${start.toFixed(3)}&end=${end.toFixed(3)}`;
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {CameraCapture,cameraMediaURL};
  else { window.CameraCapture = CameraCapture; window.cameraMediaURL = cameraMediaURL; }
})();
