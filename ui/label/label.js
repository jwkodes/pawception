/* Human annotations only. There are no model-inference endpoints on this page. */
(() => {
  const $=id=>document.getElementById(id),video=$('label-video');
  const colours=['#4f8b72','#c18b48','#8b76ac','#bd6b54','#4e88a0','#a29a43','#ba4a43'];
  const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const clone=value=>JSON.parse(JSON.stringify(value));
  const round=value=>Math.round(value*1e6)/1e6;
  let dataset=null,current=null,dirty=false,saving=false,previewEnd=null;
  const key=id=>`pawception.labels.submission.v1.${id}`;
  function forgetDraft(identifier){try{localStorage.removeItem(key(identifier));}catch{}}
  function error(message=''){$('page-error').textContent=message;$('page-error').hidden=!message;}
  async function api(path,value){
    const response=await fetch(path,value===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
    const result=await response.json();if(!response.ok)throw Error(result.error||'Could not save labels');return result;
  }
  function persist(){
    if(!current)return;dirty=true;
    try{localStorage.setItem(key(current.id),JSON.stringify({revision:current.annotation_revision,annotations:current.annotations,enabled:current.enabled,reviewed:$('reviewed-clip').checked}));
      $('save-status').textContent='Unsaved changes kept in this browser. Save draft to store them on disk, or Confirm clip when reviewed.';
    }catch{$('save-status').textContent='Browser draft storage is unavailable. Use Save draft to keep these changes on disk.';}
    renderStatus();
  }
  function renderList(){
    const confirmed=dataset.clips.filter(c=>c.enabled&&c.annotation_status==='confirmed').length,included=dataset.clips.filter(c=>c.enabled).length;
    $('overall-count').textContent=`${confirmed} / ${included}`;$('overall-progress').max=included||1;$('overall-progress').value=confirmed;
    $('all-ready').hidden=!included||confirmed!==included;$('clip-count').textContent=`${dataset.clips.length} clips`;
    $('clip-list').innerHTML=dataset.clips.map((clip,index)=>{
      const labeled=Object.values(clip.annotations).filter(a=>a.label!=='unlabeled').length;
      const status=clip.annotation_status==='confirmed'?'Confirmed':clip.annotation_status==='excluded'?'Excluded':clip.annotation_status==='draft'?'Draft':'Not reviewed';
      return `<button class="clip-choice" data-clip="${clip.id}" aria-current="${clip.id===current?.id}"><strong>${index+1}. ${escape(clip.title)}</strong><small>${clip.duration_seconds.toFixed(2)}s · <span class="${clip.annotation_status==='confirmed'?'confirmed':''}">${status}</span> · ${labeled}/${Object.keys(dataset.behaviors).length} labeled${clip.available?'':' · Video missing'}</small></button>`;
    }).join('');
  }
  function renderStatus(){
    if(!current)return;
    const labeled=Object.values(current.annotations).filter(a=>a.label!=='unlabeled').length;
    $('labels-progress').textContent=`${labeled} / ${Object.keys(dataset.behaviors).length} events labeled. Confirm negatives as well as positives.`;
    const badge=$('saved-status');badge.className=`badge ${dirty?'dirty':current.annotation_status==='confirmed'?'confirmed':''}`;
    badge.textContent=dirty?'Unsaved changes':current.annotation_status==='confirmed'?'Confirmed':current.annotation_status==='excluded'?'Excluded':current.annotation_status==='draft'?'Draft saved':'Not reviewed';
    $('save-draft').disabled=$('confirm-clip').disabled=saving||!current.available;
    $('confirm-clip').textContent=$('include-clip').checked?'Confirm clip':'Save exclusion';
    $('remaining-negative').textContent=current.has_audio?'Mark remaining events not observed':'Mark remaining visual events not observed';
  }
  function renderEvents(){
    if(!current)return;
    const events=Object.keys(dataset.behaviors).sort((a,b)=>Number(current.suggested_events.includes(b))-Number(current.suggested_events.includes(a)));
    $('event-cards').innerHTML=events.map(event=>{
      const item=current.annotations[event],ranges=item.intervals||[],colour=colours[Object.keys(dataset.behaviors).indexOf(event)%colours.length];
      const choices=[['observed','Observed'],['not_observed','Not observed'],['unassessable','Cannot tell']];
      return `<article class="event-card" data-event="${event}" style="--event-color:${colour}">
        <div class="event-heading"><i aria-hidden="true"></i><h3>${escape(dataset.behavior_titles[event]||event)}</h3><span class="mode-tag">${dataset.behavior_modes[event]==='audio'?'AUDIO':dataset.behavior_types[event]==='safety_watch'?'SAFETY WATCH':'VISUAL'}</span></div>
        <p class="event-definition">${escape(dataset.behaviors[event])}</p>
        <div class="label-choices" role="group" aria-label="${escape(dataset.behavior_titles[event])} assessment">${choices.map(([label,title])=>`<button class="choice" data-label="${label}" aria-pressed="${item.label===label}">${title}</button>`).join('')}<button class="clear-label" data-action="clear">Reset label</button></div>
        ${event==='barking'&&!current.has_audio?'<p class="audio-note">No audio track: choose Cannot tell. This video cannot establish the presence or absence of barking.</p>':''}
        ${item.label==='unassessable'?'<p class="interval-note">Excluded from accuracy scoring; this is not a negative label.</p>':''}
        ${item.label==='observed'?`<div class="interval-editor"><div class="interval-actions"><button class="secondary" data-action="add">+ Add interval</button><button class="secondary" data-action="whole">Throughout the clip</button></div>
          ${ranges.length?ranges.map((range,index)=>`<div class="interval-row" data-index="${index}"><label>Start (seconds)<input type="number" step="0.01" min="0" max="${current.duration_seconds}" data-boundary="start" value="${escape(range.start)}" aria-label="${escape(dataset.behavior_titles[event])} interval ${index+1} start"></label><button class="secondary" data-action="start">Use playhead</button><label>End (seconds)<input type="number" step="0.01" min="0" max="${current.duration_seconds}" data-boundary="end" value="${escape(range.end)}" aria-label="${escape(dataset.behavior_titles[event])} interval ${index+1} end"></label><button class="secondary" data-action="end">Use playhead</button><button class="secondary" data-action="preview">Play interval ▶</button><button class="secondary remove" data-action="remove" aria-label="Remove ${escape(dataset.behavior_titles[event])} interval ${index+1}">Remove</button></div>`).join(''):'<p class="interval-note">Add at least one time interval before confirming this event.</p>'}
          <div class="interval-track" aria-label="${escape(dataset.behavior_titles[event])} intervals; click to seek"><i class="playhead"></i></div><p class="interval-note">Separate intervals represent separate visits or bouts. Times refer to this source clip.</p></div>`:''}
        <label class="annotation-note">Evidence / annotation notes (optional)<textarea rows="2" maxlength="2000" data-note aria-label="${escape(dataset.behavior_titles[event])} annotation notes">${escape(item.basis==='Awaiting manual review; suggested event names are not ground truth.'||item.reviewed_by==='Pending user review'?'':item.basis)}</textarea></label>
      </article>`;
    }).join('');
    updateTracks();renderStatus();
  }
  function updateTracks(){
    if(!current)return;
    const cursor=Math.min(100,100*(video.currentTime||0)/current.duration_seconds);
    $('video-time').textContent=`${(video.currentTime||0).toFixed(2)} / ${current.duration_seconds.toFixed(2)}s`;
    for(const card of $('event-cards').querySelectorAll('[data-event]')){
      const track=card.querySelector('.interval-track');if(!track)continue;
      for(const range of track.querySelectorAll('.range'))range.remove();
      for(const range of current.annotations[card.dataset.event].intervals||[]){
        if(!Number.isFinite(range.start)||!Number.isFinite(range.end)||range.start<0||range.end<=range.start||range.end>current.duration_seconds)continue;
        const span=document.createElement('span');span.className='range';span.style.left=`${100*range.start/current.duration_seconds}%`;span.style.width=`${100*(range.end-range.start)/current.duration_seconds}%`;track.appendChild(span);
      }
      track.querySelector('.playhead').style.left=`${cursor}%`;
    }
  }
  function openClip(identifier,{restore=true,preservePosition=false}={}){
    const position=preservePosition?video.currentTime:0;
    video.pause();previewEnd=null;error();
    const saved=dataset.clips.find(c=>c.id===identifier);if(!saved)return;
    current=clone(saved);dirty=false;
    $('include-clip').checked=current.enabled;$('reviewed-clip').checked=current.annotation_status==='confirmed';
    $('save-status').textContent=current.annotation_status==='confirmed'?'Confirmed labels saved on disk. Editing creates a new draft until you confirm again.':'No labels are assumed from the filename or suggested focus.';
    $('save-status').classList.remove('save-error');
    if(restore)try{
      const draft=JSON.parse(localStorage.getItem(key(identifier))||'null');
      if(draft&&draft.revision===current.annotation_revision&&draft.annotations){current.annotations=draft.annotations;current.enabled=draft.enabled;$('include-clip').checked=current.enabled;$('reviewed-clip').checked=!!draft.reviewed;dirty=true;$('save-status').textContent='Unsaved draft restored from this browser. Save or confirm when ready.';}
      else if(draft)$('save-status').textContent='A newer saved version exists. The older browser draft was not applied.';
    }catch{}
    $('clip-title').textContent=current.title;$('clip-number').textContent=`CLIP ${dataset.clips.indexOf(saved)+1} OF ${dataset.clips.length} · ${current.duration_seconds.toFixed(2)} SECONDS`;
    $('suggested-focus').textContent=`Suggested focus: ${current.suggested_events.map(event=>dataset.behavior_titles[event]).join(' · ')}. Review all events below.`;
    $('audio-note').textContent=current.has_audio?'Audio track available. Unmute and listen before labeling barking.':'This clip has no audio track. Barking cannot be assessed.';
    const credit=current.credit||{};
    const creditEl=$('video-credit');creditEl.textContent=`${credit.creator||'Source footage'} · ${credit.license||'See source license'} · `;
    if(credit.source_url&&/^https:\/\//.test(credit.source_url)){const link=document.createElement('a');link.href=credit.source_url;link.target='_blank';link.rel='noopener';link.textContent='Original source';creditEl.appendChild(link);}
    const url=current.available?current.url:'';
    if(video.getAttribute('src')!==url)video.src=url;
    else if(current.available)video.currentTime=position;
    video.playbackRate=Number($('playback-speed').value);
    if(!current.available)error(current.media_error||'The local video is unavailable.');
    history.replaceState(null,'',`#${identifier}`);renderList();renderEvents();updateTracks();
  }
  $('clip-list').addEventListener('click',event=>{const button=event.target.closest('[data-clip]');if(button)openClip(button.dataset.clip);});
  $('event-cards').addEventListener('click',event=>{
    const card=event.target.closest('[data-event]');if(!card||!current)return;
    const item=current.annotations[card.dataset.event],button=event.target.closest('button');
    if(event.target.closest('.interval-track')){const track=event.target.closest('.interval-track'),rect=track.getBoundingClientRect();previewEnd=null;video.currentTime=Math.max(0,Math.min(current.duration_seconds,(event.clientX-rect.left)/rect.width*current.duration_seconds));return;}
    if(!button)return;
    if(button.dataset.label){item.label=button.dataset.label;if(item.label!=='observed')item.intervals=[];}
    else{
      const ranges=item.intervals||(item.intervals=[]),row=button.closest('[data-index]'),index=Number(row?.dataset.index);
      switch(button.dataset.action){
        case 'clear':item.label='unlabeled';item.intervals=[];break;
        case 'add':{const start=round(Math.max(0,Math.min(video.currentTime,current.duration_seconds-1/(current.fps||25))));ranges.push({start,end:round(Math.min(current.duration_seconds,start+1))});break;}
        case 'whole':item.intervals=[{start:0,end:current.duration_seconds}];break;
        case 'remove':ranges.splice(index,1);break;
        case 'start':ranges[index].start=round(video.currentTime);break;
        case 'end':ranges[index].end=round(Math.min(current.duration_seconds,video.currentTime));break;
        case 'preview':{const range=ranges[index];if(!Number.isFinite(range.start)||!Number.isFinite(range.end)||range.end<=range.start||range.start<0||range.end>current.duration_seconds){error('Adjust the interval so 0 ≤ start < end ≤ clip duration.');return;}error();video.currentTime=range.start;previewEnd=range.end;video.play().catch(()=>{});return;}
        default:return;
      }
    }
    persist();renderEvents();
  });
  $('event-cards').addEventListener('input',event=>{
    const card=event.target.closest('[data-event]');if(!card||!current)return;
    const item=current.annotations[card.dataset.event];
    if(event.target.matches('[data-note]'))item.basis=event.target.value;
    else if(event.target.matches('[data-boundary]')){const index=Number(event.target.closest('[data-index]').dataset.index);item.intervals[index][event.target.dataset.boundary]=event.target.value===''?null:Number(event.target.value);updateTracks();}
    else return;persist();
  });
  $('remaining-negative').addEventListener('click',()=>{
    if(!current)return;
    for(const [event,item] of Object.entries(current.annotations))if(item.label==='unlabeled'&&(current.has_audio||dataset.behavior_modes[event]!=='audio')){item.label='not_observed';item.intervals=[];}
    persist();renderEvents();
  });
  for(const id of ['include-clip','reviewed-clip'])$(id).addEventListener('change',()=>{if(!current)return;current.enabled=$('include-clip').checked;persist();});
  for(const [id,direction] of [['previous-frame',-1],['next-frame',1]])$(id).addEventListener('click',()=>{if(!current)return;video.pause();previewEnd=null;const frame=Math.round(video.currentTime*(current.fps||25))+direction;video.currentTime=Math.max(0,Math.min(current.duration_seconds,frame/(current.fps||25)));});
  $('playback-speed').addEventListener('change',()=>{video.playbackRate=Number($('playback-speed').value);});
  $('sound').addEventListener('click',()=>{video.muted=!video.muted;});
  video.addEventListener('volumechange',()=>{$('sound').textContent=video.muted?'🔇 Sound off':'🔊 Sound on';$('sound').setAttribute('aria-label',video.muted?'Unmute review video':'Mute review video');});
  video.addEventListener('pointerdown',()=>{previewEnd=null;});
  video.addEventListener('timeupdate',()=>{if(previewEnd!==null&&video.currentTime>=previewEnd){video.pause();previewEnd=null;}updateTracks();});
  video.addEventListener('loadedmetadata',updateTracks);
  async function save(confirm){
    if(!current||saving)return;error();saving=true;renderStatus();const identifier=current.id;
    try{
      dataset=await api(`/api/labels/${identifier}`,{revision:current.annotation_revision,confirm,enabled:$('include-clip').checked,reviewed:$('reviewed-clip').checked,annotations:current.annotations});
      forgetDraft(identifier);openClip(identifier,{restore:false,preservePosition:true});
      $('save-status').textContent=confirm?(current.enabled?'Clip confirmed and saved. Confirming labels does not start evaluation.':'Clip excluded and saved.'):'Draft saved on disk. It will not be scored as confirmed ground truth.';
    }catch(problem){error(problem.message);$('save-status').textContent=`Not saved: ${problem.message} Your draft remains available for correction.`;$('save-status').classList.add('save-error');}
    finally{saving=false;renderStatus();}
  }
  $('save-draft').addEventListener('click',()=>save(false));$('confirm-clip').addEventListener('click',()=>save(true));
  $('reload-saved').addEventListener('click',async()=>{if(!current)return;const identifier=current.id;try{dataset=await api('/api/labels');forgetDraft(identifier);openClip(identifier,{restore:false,preservePosition:true});}catch(problem){error(problem.message);}});
  $('next-clip').addEventListener('click',()=>{if(!current)return;const index=dataset.clips.findIndex(c=>c.id===current.id);openClip(dataset.clips[(index+1)%dataset.clips.length].id);});
  $('export').addEventListener('click',async()=>{try{const data=await api('/api/labels/export'),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=`${data.dataset_id}.revision-${data.revision}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(problem){error(problem.message);}});
  (async()=>{dataset=await api('/api/labels');const identifier=location.hash.slice(1);openClip(dataset.clips.some(c=>c.id===identifier)?identifier:dataset.clips.find(c=>c.enabled&&c.annotation_status!=='confirmed')?.id||dataset.clips[0].id);})().catch(problem=>error(problem.message));
})();
