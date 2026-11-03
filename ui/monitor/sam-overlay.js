/* Masks are drawn only over their exact source-frame time interval. */
const SamTracks = (() => {
  function frameAt(frames,time) {
    let left=0,right=frames.length;
    while (left<right) { const middle=(left+right)>>>1; if (frames[middle].start<=time) left=middle+1; else right=middle; }
    const frame=frames[left-1];
    return frame && time<frame.end ? frame : null;
  }

  class Overlay {
    constructor(video,{sourceTime=()=>video.currentTime}={}) {
      this.video=video; this.sourceTime=sourceTime; this.entries=new Map(); this.images=new Map();
      this.image=document.createElement('img'); this.image.className='sam-video-overlay';
      this.image.alt='Pet segmentation'; this.image.hidden=true;
      video.parentElement.append(this.image);
      this.closed=false;
      this.tick=()=>{this.draw(); if (!this.closed) this.animation=requestAnimationFrame(this.tick);};
      this.tick();
    }

    update(alerts) {
      const tracks=alerts.map(alert=>alert.visual_evidence?.tracking).filter(Boolean);
      const urls=new Set(tracks.map(track=>track.url));
      for (const url of this.entries.keys()) if (!urls.has(url)) this.entries.delete(url);
      for (const track of tracks) {
        let entry=this.entries.get(track.url);
        if (!entry) { entry={frames:[],version:null}; this.entries.set(track.url,entry); }
        entry.start=track.start_seconds;
        const version=`${track.revision || track.frames_received}:${track.status}`;
        if (entry.version===version || entry.loading) continue;
        entry.loading=true;
        fetch(track.url).then(response=>{if (!response.ok) throw new Error('Track unavailable'); return response.json();})
          .then(manifest=>{
            if (this.closed || this.entries.get(track.url)!==entry) return;
            entry.frames=manifest.frames; entry.version=version; this.draw();
          }).catch(()=>{}).finally(()=>{entry.loading=false;});
      }
      this.draw();
    }

    draw() {
      if (this.closed) return;
      const time=this.sourceTime();
      let url=null;
      for (const [manifest,entry] of this.entries) {
        const frame=frameAt(entry.frames,time-entry.start);
        if (frame) { url=manifest.replace('tracking.json',`frames/${frame.file}?v=${frame.revision || 0}`); break; }
      }
      if (!url || this.video.seeking) { this.image.hidden=true; return; }
      let cached=this.images.get(url);
      if (!cached) {
        cached=new Image(); cached.src=url; this.images.set(url,cached);
        if (this.images.size>80) this.images.delete(this.images.keys().next().value);
      }
      if (!cached.complete || !cached.naturalWidth) { this.image.hidden=true; return; }
      if (this.image.getAttribute('src')!==url) this.image.src=url;
      this.image.hidden=false;
    }

    destroy() { this.closed=true; cancelAnimationFrame(this.animation); this.entries.clear(); this.images.clear(); this.image.remove(); }
  }
  return {frameAt,Overlay};
})();
if (typeof module!=='undefined' && module.exports) module.exports=SamTracks;
