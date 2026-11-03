/* Saved-video analysis; loading and filtering never initiate model requests. */
const OfflineTools = (() => {
  function parseTime(text) {
    if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$/.test(String(text).trim())) throw new Error('Enter a time as mm:ss, hh:mm:ss, or seconds.');
    const parts = String(text).trim().split(':').map(Number);
    if (parts.length>1 && parts.slice(1).some(part=>part>=60)) throw new Error('Minutes and seconds after a colon must be below 60.');
    return parts.reduce((total,part)=>total*60+part,0);
  }
  function clock(value) {
    const seconds = Math.floor(value%60).toString().padStart(2,'0');
    const minutes = Math.floor(value/60)%60;
    return value>=3600 ? `${Math.floor(value/3600)}:${String(minutes).padStart(2,'0')}:${seconds}` : `${Math.floor(value/60).toString().padStart(2,'0')}:${seconds}`;
  }
  function windowCount(start,end,size,overlap=0) {
    if (!(start>=0 && end>start && size>0 && overlap>=0 && overlap<size)) throw new Error('Choose a valid time range, window length and overlap.');
    return Math.max(1,1+Math.ceil(Math.max(0,end-start-size)/(size-overlap)-1e-9));
  }
  function cellAt(x,y,width,height,columns,rows) {
    return Math.max(0,Math.min(rows-1,Math.floor(y/height*rows)))*columns+
      Math.max(0,Math.min(columns-1,Math.floor(x/width*columns)));
  }
  function normalizeEvents(value) {
    if (!Array.isArray(value) || value.length>20) return [];
    return value.filter(event=>event && typeof event.name==='string' && event.name.trim() && event.name.length<=80 &&
      typeof event.description==='string' && event.description.trim() && event.description.length<=1000 &&
      ['auto','visual','audio','both'].includes(event.input_mode||'auto')).map(event=>({name:event.name.trim(),description:event.description.trim(),input_mode:event.input_mode||'auto'}));
  }
  function deleteTrash(removed,id) {return removed.filter(record=>record.id!==id);}
  function visibleRuns(runs,source,provider,kind,method='sam') {
    const expected=kind==='hotspots' && method==='changes' ? 'local' : provider;
    return runs.filter(run=>run.source_id===source && run.kind===kind && run.provider===expected)
      .sort((a,b)=>a.created_at.localeCompare(b.created_at));
  }
  function searchOutcome(run) {
    const matches=(run.detections||[]).length,windows=run.windows||[];
    const failed=windows.filter(window=>['partial','error','invalid','cancelled'].includes(window.status)).length;
    const done=windows.filter(window=>window.status==='completed').length;
    if(run.status==='completed' && !failed)return matches ? `${matches} matching ${matches===1?'interval':'intervals'} detected.` : 'No matching events detected.';
    if(failed)return `${matches} matching intervals detected; ${failed} windows have incomplete or failed checks. Unchecked footage has no result.`;
    return `${matches} matching intervals detected so far · ${done}/${windows.length} windows complete.`;
  }
  function currentSearch(searches,id) {return searches.find(run=>run.id===id)||searches.at(-1)||null;}
  function durationLabel(seconds) {return seconds<60 ? `${Number(seconds.toFixed(seconds<1?2:1))}s` : clock(seconds);}
  function hotspotWarning(run,heat) {
    if(run?.hotspot_method!=='changes' || !heat?.sample_count)return '';
    const fraction=heat.position_samples/heat.sample_count;
    return fraction<0.05 ? `Only ${heat.position_samples} of ${heat.sample_count} analysed samples yielded a usable foreground position (${(fraction*100).toFixed(1)}%). Evidence is sparse; these squares do not establish where the pet spent its time. A mostly stationary or partly out-of-frame pet can blend into the background.` : '';
  }
  function samProgress(run) {
    const value=run?.frame_progress;
    if(run?.kind!=='hotspots' || run.hotspot_method==='changes' || !value?.total)return null;
    const received=Math.max(0,Math.min(value.total,value.received));
    return {...value,received,percent:Math.floor(received/value.total*100)};
  }
  function localProgress(run) {
    if(run?.hotspot_method!=='changes')return null;
    const value=run.local_progress || (run.status==='completed' && run.sampling ? {stage:'comparing_frames',completed:run.sampling.sample_count,total:run.sampling.sample_count} : null);
    return value?.total ? {...value,percent:Math.floor(value.completed/value.total*100)} : null;
  }
  function completionText(run,now=Date.now()) {
    if(!run)return '';
    if(run.status==='completed')return typeof run.elapsed_seconds==='number' ? `Finished in ${durationLabel(run.elapsed_seconds)}.` : '';
    if(['error','stopped','interrupted'].includes(run.status))return typeof run.elapsed_seconds==='number' ? `Elapsed: ${durationLabel(run.elapsed_seconds)}.` : '';
    if(run.status==='stopping')return 'Stopping after the active work cleans up.';
    const eta=run.estimated_remaining_seconds;
    if(typeof eta!=='number' && typeof run.estimated_stage_remaining_seconds==='number')return `Background ready in about ${Math.max(1,Math.ceil(run.estimated_stage_remaining_seconds))}s, then frame comparison. Overall completion: calculating…`;
    if(typeof eta!=='number')return 'Estimated completion: calculating from observed throughput…';
    const duration=eta<1 ? 'less than 1s' : eta<60 ? `${Math.ceil(eta)}s` : eta<3600 ? `${Math.ceil(eta/60)} min` : `${(eta/3600).toFixed(1)} h`;
    const clockTime=new Date(now+eta*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    return `About ${duration} remaining · around ${clockTime}. Estimate updates as processing continues.`;
  }
  return {parseTime,clock,windowCount,cellAt,normalizeEvents,deleteTrash,visibleRuns,searchOutcome,currentSearch,durationLabel,hotspotWarning,samProgress,localProgress,completionText};
})();
if (typeof module!=='undefined' && module.exports) module.exports=OfflineTools;
if (typeof document!=='undefined') (() => {
  const $=id=>document.getElementById(id), {parseTime,clock,windowCount,cellAt,durationLabel}=OfflineTools;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const review=new ActivityReview(), runs=new Map();
  let sources=[], selected=null, active=null, heatRun=null, heat=null, heatFilter=null, cell=0, loading=false, polling=false, revision=0, heatmapKeyboard=false;
  let searchId=null;
  let viewedJobId=null;
  let heatRevision=0;
  let maskFrameKey=null;
  let heatmapView='overlay';
  let workspace='events';
  let hotspotView='2d';
  try {const saved=localStorage.getItem('pawception.offline.hotspot-view');if(['2d','3d'].includes(saved))hotspotView=saved;} catch {}
  try {const saved=localStorage.getItem('pawception.offline.workspace');if(['events','hotspots'].includes(saved))workspace=saved;} catch {}
  try {const saved=localStorage.getItem('pawception.offline.heatmap-view');if(['overlay','heatmap','image'].includes(saved))heatmapView=saved;} catch {}
  const spatialKey='pawception.offline.spatial.v1';
  try {
    const saved=JSON.parse(localStorage.getItem(spatialKey)||'{}');
    if([80,160,320,640].includes(saved.change_long_edge))$('change-long-edge').value=saved.change_long_edge;
    if(Number.isInteger(saved.cell_size) && saved.cell_size>=16 && saved.cell_size<=256)$('heatmap-cell-size').value=saved.cell_size;
  } catch {}
  const draftKey='pawception.recording.events.v1';
  let drafts=[],removed=[],editing=null;
  try {
    const saved=JSON.parse(localStorage.getItem(draftKey)||'{}');drafts=OfflineTools.normalizeEvents(saved.events);
    removed=Array.isArray(saved.removed) ? saved.removed.filter(record=>typeof record.id==='string' && OfflineTools.normalizeEvents([record.event]).length) : [];
  } catch {}
  const inputLabel=mode=>({auto:'Smart Routing',visual:'Visual',audio:'Audio',both:'Visual + Audio'})[mode||'auto'];
  const runEvents=run=>run.events?.length ? run.events : [{id:run.id,name:run.description,description:run.description}];
  const modeKey='pawception.offline.provider';
  try {const saved=localStorage.getItem(modeKey);if(['mock','muse'].includes(saved))$('provider').value=saved;} catch {}
  function renderAnalysisMode() {
    const real=$('provider').value==='muse';
    $('mode-badge').textContent=real ? 'AI ANALYSES · MUSE + SAM' : 'AI ANALYSES · MOCK';
    $('search-button').toggleAttribute('data-muse',real);
    $('hotspots-button').toggleAttribute('data-muse',real && $('hotspot-method').value==='sam');
  }
  renderAnalysisMode();
  function saveDrafts() {
    try {localStorage.setItem(draftKey,JSON.stringify({events:drafts,removed}));} catch {error('Events could not be saved in this browser.');}
    renderDrafts();plan();render();
  }
  function renderDrafts() {
    $('draft-count').textContent=`${drafts.length} ${drafts.length===1?'event':'events'}`;
    $('event-drafts').innerHTML=drafts.map((event,index)=>`<article class="event-card" style="${eventStyle({palette_index:index})}"><span class="event-icon">${String(index+1).padStart(2,'0')}</span><div class="event-copy"><h3>${escape(event.name)}</h3><p>${escape(event.description)}</p><p class="event-threshold">${inputLabel(event.input_mode)}</p></div><div class="offline-event-actions"><button type="button" data-edit="${index}" aria-label="Edit ${escape(event.name)}">Edit</button><button type="button" data-remove="${index}" aria-label="Remove ${escape(event.name)}">Remove ×</button></div></article>`).join('') || '<p class="field-help">Add your first event to get started.</p>';
    $('add-recording-event').disabled=drafts.length>=20;
    $('event-trash').hidden=!removed.length;$('trash-count').textContent=`(${removed.length})`;
    $('trash-events').innerHTML=removed.slice().reverse().map(record=>`<article class="removed-event"><div><strong>${escape(record.event.name)}</strong><p>${escape(record.event.description)}</p></div><button type="button" class="restore-event" data-restore="${escape(record.id)}" ${drafts.length>=20?'disabled':''}>Restore</button><button type="button" class="secondary-button" data-delete="${escape(record.id)}">Delete forever</button></article>`).join('');
  }
  function openEditor(index=null) {
    descriptionSuggestions.reset();
    editing=index;const event=index===null ? {name:'',description:'',input_mode:'auto'} : drafts[index];
    $('recording-event-name').value=event.name;$('recording-event-description').value=event.description;
    $('recording-suggestion-cost').textContent=$('provider').value==='mock' ? 'Mock templates · No credits' : 'Muse Spark 1.3 Contributor · Text only · Uses credits';
    $('suggest-recording-description').toggleAttribute('data-muse',$('provider').value==='muse');
    $('recording-event-mode').value=event.input_mode||'auto';$('recording-event-dialog').showModal();
  }
  function error(message='') { $('offline-error').textContent=message; $('offline-error').hidden=!message; }
  async function api(url,value) {
    const response=await fetch(url,value===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
    const result=await response.json();
    if (!response.ok) throw new Error(result.error||'Could not analyse the recording.');
    return result;
  }
  function range() {
    const start=parseTime($('range-start').value),end=parseTime($('range-end').value);
    if (!selected || end<=start || end>selected.duration_seconds+0.001) throw new Error('Choose a range inside the loaded recording.');
    return {start_seconds:start,end_seconds:end};
  }
  function plan() {
    try {
      const {start_seconds:start,end_seconds:end}=range();
      const events=windowCount(start,end,Number($('event-window').value),Number($('event-overlap').value));
      const budget=Math.min(Number($('max-frames').value),Math.max(1,Math.ceil((end-start)*selected.fps)));
      const tracking=Math.ceil(budget/250), interval=(end-start)/budget;
      const mock=$('provider').value==='mock',changes=$('hotspot-method').value==='changes';
      const imageScale=Math.min(1,Number($('change-long-edge').value)/Math.max(selected.width,selected.height));
      const imageWidth=Math.max(1,Math.round(selected.width*imageScale)),imageHeight=Math.max(1,Math.round(selected.height*imageScale));
      $('change-size-help').textContent=`This recording: ${imageWidth}×${imageHeight} · ${(1/imageScale).toFixed(1)}× spatial downsampling. Applies to the next local analysis. Larger images use more memory and time.`;
      const cellSize=Number($('heatmap-cell-size').value);
      $('heatmap-size-help').textContent=Number.isInteger(cellSize) && cellSize>=16 && cellSize<=256 ? `${Math.ceil(selected.width/cellSize)} columns × ${Math.ceil(selected.height/cellSize)} rows. Smaller squares give a finer grid. Updates saved results without rerunning detection.` : 'Choose a square size from 16 to 256 source pixels.';
      $('pet-prompt-field').hidden=changes;$('pet-prompt').required=!changes;
      $('hotspot-method-help').textContent=changes ? 'For a fixed camera with one moving pet. Compares sampled frames with a median background. People, shadows or other movement can be mistaken for the pet; a pet staying in one place for most of the recording may blend into the background.' : 'Use a fixed camera. SAM locates the pet by description; multiple simultaneous matches are excluded.';
      $('search-plan').textContent=mock ? `${drafts.length} ${drafts.length===1?'event':'events'} · ${events} mock ${events===1?'window':'windows'} · No API calls.` : `${drafts.length} ${drafts.length===1?'event':'events'} · ${events} ${events===1?'window':'windows'} · Smart Routing routes each Auto event once; requests are grouped by model.`;
      $('hotspots-plan').textContent=`Up to ${budget} frames · approximately ${interval.toFixed(2)}s between samples · ${changes?'Local processing, no API calls':`${tracking} ${mock?'mock batches, no API calls':'SAM requests'}`}. ${interval>0.1?'Dwell times are estimates; short visits can be missed.':''}`;
    } catch(e) { $('search-plan').textContent=$('hotspots-plan').textContent=e.message; }
    renderAnalysisMode();
    $('audio-model-warning').hidden=!$('audio-model').value.startsWith('muse-spark-1.3');
  }
  function render() {
    if (!selected) return;
    const provider=$('provider').value,mode=provider==='muse'?'Muse':'Mock';
    const searches=OfflineTools.visibleRuns([...runs.values()],selected.id,provider,'events');
    const current=OfflineTools.currentSearch(searches,searchId),previous=searchId;
    searchId=current?.id||null;
    $('search-run').innerHTML=searches.length ? searches.map((run,index)=>`<option value="${run.id}">${index===searches.length-1?'Latest search':`Search ${index+1}`} · ${escape(run.description)} · ${clock(run.start_seconds)}–${clock(run.end_seconds)}</option>`).reverse().join('') : '<option value="">No searches yet</option>';
    $('search-run').value=searchId||'';$('search-run').disabled=!searches.length;
    const windows=current?.windows||[];
    const events=(current ? runEvents(current) : []).map((event,index)=>({...event,name:`${provider==='mock'?'[Mock] ':''}${event.name}`,palette_index:index}));
    const detections=(current?.detections||[]).map(part=>({...part,source:'custom'}));
    review.update({id:selected.id,duration:selected.duration_seconds,captured_seconds:selected.duration_seconds,
      source_type:'replay',offline:true,provider,
      windows,events,detections,alerts:[],safety_watch:false,skipped_windows:[],failed_windows:windows.filter(w=>w.status==='error')},
      {url:selected.url},new Set());
    if(previous!==searchId){review.selected=null;review.showContext(null);}
    $('review-title').textContent=`${mode} search results`;
    $('search-results-note').textContent=current ? `Showing one ${mode} search: ${current.description} · ${clock(current.start_seconds)}–${clock(current.end_seconds)}. Select an earlier search to review its results.` : `No ${mode} searches for this recording yet. Add events above, then choose Search recording.`;
    $('search-history').innerHTML=searches.length ? searches.slice().reverse().map(run=>`<div class="search-result"><p><strong>${escape(run.description)}</strong> · ${clock(run.start_seconds)}–${clock(run.end_seconds)} · ${escape(run.status)}<br>${escape(OfflineTools.searchOutcome(run))}</p><div><button type="button" class="secondary-button" data-search="${run.id}" ${run.id===searchId?'disabled':''}>${run.id===searchId?'Shown on timeline':'Show on timeline'}</button>${(run.detections||[]).map((part,i)=>`<button type="button" class="secondary-button" data-run="${run.id}" data-match="${i}">${escape(runEvents(run).find(event=>event.id===part.event_id)?.name)} · ${clock(part.start)}–${clock(part.end)} ▶</button>`).join('')}</div>${observationDetails(run)}</div>`).join('') : `<p>No ${mode} searches for this recording yet.</p>`;
    const hotspotRuns=OfflineTools.visibleRuns([...runs.values()],selected.id,provider,'hotspots',$('hotspot-method').value);
    const choice=heatRun?.id||$('heatmap-run').value;
    $('heatmap-run').innerHTML='<option value="">Choose an analysis</option>'+hotspotRuns.map(run=>`<option value="${run.id}">${escape(run.pet_prompt)} · ${escape(run.provider==='mock'?'Mock':run.hotspot_method==='changes'?'Change detection':'SAM')} · ${escape(run.status)} · ${clock(run.start_seconds)}–${clock(run.end_seconds)}</option>`).join('');
    $('heatmap-run').value=hotspotRuns.some(run=>run.id===choice) ? choice : hotspotRuns.at(-1)?.id||'';
    const next=runs.get($('heatmap-run').value);
    if (next && heatRun?.id!==next.id) chooseHeatmap(next);
    else if (!next) { heatRun=heat=heatFilter=null; $('heat-filter').hidden=true; drawHeatmap(); }
    const busy=active && ['queued','running','stopping'].includes(active.status);
    const spatialBusy=[...runs.values()].some(run=>['queued','running','stopping'].includes(run.spatial?.status));
    $('search-button').disabled=loading||!selected||busy||spatialBusy||!drafts.length;
    $('hotspots-button').disabled=loading||!selected||busy||spatialBusy;
    $('stop-job').hidden=!busy;
    $('stop-job').disabled=active?.status==='stopping';
    const visible=(workspace==='events'?searches:hotspotRuns).slice().sort((a,b)=>a.created_at.localeCompare(b.created_at));
    const shown=busy ? active : visible.find(run=>run.id===viewedJobId)||visible.at(-1);
    $('sam-progress-note').hidden=true;$('job-progress').removeAttribute('aria-valuetext');
    $('job-eta').textContent=OfflineTools.completionText(shown);$('job-eta').hidden=!$('job-eta').textContent;
    if (shown) {
      const done=shown.windows.filter(w=>['completed','partial','error','invalid'].includes(w.status)).length;
      const other=busy && !visible.some(run=>run.id===shown.id);
      $('job-title').textContent=`${shown.provider==='mock'?'Mock':shown.provider==='local'?'Local':'Muse'} · ${shown.kind==='events'?'Event search':'Hotspot analysis'}${other?' running in the background':''} · ${done}/${shown.windows.length} ${shown.kind==='events'?'windows':'batches'}`;
      $('job-status').textContent=(shown.kind==='events' && shown.status==='completed' ? OfflineTools.searchOutcome(shown) : shown.message)+(shown.error ? ` (${shown.error.status_code||shown.error.type})` : '');
      $('job-progress').hidden=false; $('job-progress').value=done; $('job-progress').max=shown.windows.length;
      if(shown.hotspot_method==='changes') {
        const status=({queued:'Queued',running:'Analysing',stopping:'Stopping',completed:'Complete',stopped:'Stopped',error:'Failed',interrupted:'Interrupted'})[shown.status]||shown.status;
        $('job-title').textContent=`Local change detection · ${status}${other?' in the background':''}`;
        $('job-progress').hidden=!['queued','running','stopping'].includes(shown.status);
        $('job-progress').removeAttribute('value');
        const progress=OfflineTools.localProgress(shown);
        if(progress){
          const labels={reading_frames:'Reading sampled frames',building_background:'Building background',comparing_frames:'Comparing frames'};
          if(shown.status==='running')$('job-status').textContent=({reading_frames:'Reading uniformly sampled frames from the recording.',building_background:'Estimating the median background from the sampled frames.',comparing_frames:'Locating foreground changes in each sampled frame.'}[progress.stage]||shown.message)+' No API calls.';
          const unit=progress.stage==='building_background'?'rows':'frames';
          $('job-title').textContent=shown.status==='completed' ? `Local change detection · Complete · ${progress.total.toLocaleString()} frames checked` : `Local change detection · ${labels[progress.stage]||status} · ${progress.completed.toLocaleString()}/${progress.total.toLocaleString()} ${unit} · ${progress.percent}% of this stage`;
          $('job-progress').hidden=false;$('job-progress').max=progress.total;$('job-progress').value=progress.completed;
          $('job-progress').setAttribute('aria-valuetext',`${labels[progress.stage]||status}: ${progress.completed} of ${progress.total} ${unit}`);
        }
      } else {
        const progress=OfflineTools.samProgress(shown);
        if(progress){
          const name=shown.provider==='mock'?'Mock':'SAM';
          const stages={queued:'Queued',preparing:'Preparing sampled video',uploading:'Uploading SAM input',waiting:'Waiting for SAM frame results',receiving:'Receiving SAM frame results',results_ready:'SAM results ready',cleanup:'Cleaning up temporary upload',batch_complete:'Preparing next batch',completed:'Analysis complete',stopped:'Stopped; partial results retained',interrupted:'Interrupted; saved results retained',error:'Analysis failed; partial previews retained'};
          const stage=shown.status==='stopping'?'Stopping after cleanup':stages[progress.stage]||shown.message;
          $('job-title').textContent=`${name} hotspots · ${progress.received.toLocaleString()}/${progress.total.toLocaleString()} sampled frames · ${progress.percent}%`;
          $('job-status').textContent=shown.provider==='mock' ? shown.message : `${stage} · ${progress.completed_batches}/${progress.total_batches} batches complete.`;
          $('job-progress').max=progress.total;$('job-progress').value=progress.received;
          $('job-progress').setAttribute('aria-valuetext',`${progress.received} of ${progress.total} sampled frames, ${progress.percent}%`);
          $('sam-progress-note').hidden=shown.provider==='mock';
        }
      }
    } else {
      $('job-title').textContent='Ready when you are.';
      $('job-status').textContent=`No saved ${mode} analysis in this view. Choose an analysis to begin.`;
      $('job-progress').hidden=true;
    }
  }
  function showSearch(id) {
    searchId=id;viewedJobId=id;setWorkspace('events');review.selected=null;review.hideHover();review.showContext(null);
  }
  function setWorkspace(kind) {
    workspace=kind;
    for(const panel of document.querySelectorAll('[data-analysis]'))panel.hidden=panel.dataset.analysis!==kind;
    for(const tab of $('analysis-tabs').querySelectorAll('[role=tab]')){
      const selected=tab.dataset.workspace===kind;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;
    }
    if(kind==='events')$('hotspot-review-video').pause();else review.video.pause();
    try {localStorage.setItem('pawception.offline.workspace',kind);} catch {}
    render();
  }
  function observationDetails(run) {
    if(!run.observations?.length)return '';
    return `<details class="search-observations"><summary>${run.provider==='mock'?'Mock':'Muse'} observations · ${run.observations.length} ${run.observations.length===1?'check':'checks'}</summary>${run.observations.map(item=>`<article><strong>${escape(runEvents(run).find(event=>event.id===item.event_id)?.name)} · ${item.detected?'Detected':'Not detected'}</strong><span>${clock(item.start_seconds)}–${clock(item.end_seconds)}</span><p>${escape(item.evidence)}</p><button type="button" class="secondary-button" data-search="${run.id}" data-review="${item.start_seconds}">Review this window ▶</button></article>`).join('')}</details>`;
  }
  function setHotspotView(view) {
    hotspotView=view;
    for(const panel of document.querySelectorAll('[data-hotspot-panel]'))panel.hidden=panel.dataset.hotspotPanel!==view;
    for(const tab of $('hotspot-view-tabs').querySelectorAll('[role=tab]')){
      const selected=tab.dataset.hotspotView===view;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;
    }
    $('hotspot-review-video').pause();$('hotspot-review').hidden=true;
    try {localStorage.setItem('pawception.offline.hotspot-view',view);} catch {}
    drawSpatial();
  }
  function chooseHeatmap(run) {
    heatRun=run;heat=run.heatmap||null;heatFilter=null;
    $('spatial-fov').value=run.spatial?.settings?.fov??'';
    $('spatial-voxel').value=run.spatial?.settings?.voxel_size??.08;
    maskFrameKey=null;$('mask-follow').checked=true;
    $('heat-start').value=clock(run.start_seconds);$('heat-end').value=clock(run.end_seconds);
    $('heat-filter').hidden=false;cell=0;drawHeatmap();
    refreshHeatmap().catch(e=>error(e.message));
  }
  function heatmapQuery() {
    const size=Number($('heatmap-cell-size').value);
    if(!Number.isInteger(size) || size<16 || size>256)throw new Error('Choose a heatmap square size from 16 to 256 pixels.');
    return `?cell_size=${size}${heatFilter ? `&start=${heatFilter.start}&end=${heatFilter.end}` : ''}`;
  }
  async function refreshHeatmap() {
    if(!heatRun)return;
    const id=heatRun.id,version=++heatRevision;
    const result=await api(`/api/offline/${id}${heatmapQuery()}`);
    if(heatRun?.id===id && version===heatRevision){heatRun=result;heat=result.heatmap;cell=0;drawHeatmap();}
  }
  function drawHeatmap() {
    drawSpatial();
    $('heatmap').dataset.view=heatmapView;
    for(const button of $('heatmap-views').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.view===heatmapView));
    $('heatmap-help').hidden=heatmapView==='image';$('heatmap-legend').hidden=heatmapView==='image';
    $('heatmap-keyboard').hidden=heatmapView==='image';
    const canvas=$('heatmap-canvas'),ctx=canvas.getContext('2d');
    canvas.width=selected?.width||854;canvas.height=selected?.height||480;
    ctx.clearRect(0,0,canvas.width,canvas.height);
    $('seen-label').textContent=heatRun?.hotspot_method==='changes' ? 'Foreground located' : 'Pet seen';
    $('seen-time').textContent=heat ? `${heat.estimated?'~':''}${durationLabel(heat.seen_seconds)}` : '—';
    $('not-seen-time').textContent=heat ? `${heat.estimated?'~':''}${durationLabel(heat.not_detected_seconds)}` : '—';
    $('unprocessed-time').textContent=heat ? durationLabel(heat.unprocessed_seconds) : '—';
    $('heatmap-note').textContent=!heatRun ? 'Run a hotspot analysis to see where your pet spends time.' : heatRun.provider==='mock'
      ? 'MOCK EXAMPLE · These locations and coverage gaps are synthetic, not observations of this recording.'
      : heatRun.hotspot_method==='changes' ? 'Local change detection · Foreground locations, not verified pet sightings. People, shadows and camera movement can affect results. A mostly stationary pet may blend into the median background. No API calls.'
      : `SAM tracking · ${heatRun.pet_prompt} · ${heatRun.ambiguous_frames||0} ambiguous frames excluded.${heatRun.ambiguous_frames ? ' Multiple matches were found: use a more specific description of your pet, such as orange tabby cat, to exclude toys or other animals.' : ''}`;
    if (heatRun?.sampling) $('heatmap-note').textContent+=` ${heatRun.sampling.sample_count}/${heatRun.sampling.source_frames} frames used. ${heat?.estimated?'Time estimates use uniformly spaced samples.':'All source frames in this range were used.'}`;
    if(heat)$('heatmap-note').textContent+=` Heatmap grid: ${heat.columns}×${heat.rows}.`;
    if(heatRun?.change_resolution)$('heatmap-note').textContent+=` Detection image: ${heatRun.change_resolution.width}×${heatRun.change_resolution.height}.`;
    if(heat?.sample_count)$('heatmap-note').textContent+=` Usable samples: ${heat.position_samples}/${heat.sample_count}.`;
    if(heat)$('heatmap-note').textContent+=heat.occupancy_mode==='footprint'
      ? ' Each square counts time whenever the detected mask overlaps it. Several squares can count the same moment, so percentages can total more than 100%.'
      : ' Older results contain centre points, not full footprints. Rebuild from cached masks or rerun this analysis to show occupied area.';
    $('hotspot-warning').textContent=OfflineTools.hotspotWarning(heatRun,heat);
    $('hotspot-warning').hidden=!$('hotspot-warning').textContent;
    drawMaskResults();
    const local=heatRun?.hotspot_method==='changes',backgroundUrl=heatRun?.background_url;
    $('change-background').hidden=!local;
    $('change-background-image').hidden=!backgroundUrl;
    if(backgroundUrl){
      $('change-background-image').src=backgroundUrl;
      $('change-background-note').textContent=`The background used for this analysis, built from ${heatRun.background_reference_frames||31} reference frames across ${clock(heatRun.start_seconds)}–${clock(heatRun.end_seconds)}. Anything visible here can be treated as background, including a stationary pet. Filtering the heatmap does not rebuild this image.`;
    } else {
      $('change-background-image').removeAttribute('src');
      $('change-background-note').textContent=heatRun && ['queued','running','stopping'].includes(heatRun.status) ? 'The background will appear here once it has been estimated.' : 'No background image was saved for this older analysis. Run a new local analysis to inspect its background.';
    }
    $('heatmap-tooltip').hidden=true;
    $('heatmap-cell').hidden=true;
    if (!heat) { $('hotspot-ranking').innerHTML='';return; }
    const maximum=Math.max(...heat.cells.map(cell=>cell.seconds),0);
    const width=canvas.width/heat.columns,height=canvas.height/heat.rows;
    heat.cells.forEach((item,index)=>{
      if (!item.seconds) return;
      const strength=Math.sqrt(item.seconds/maximum);
      const channel=offset=>Math.round(255*Math.max(0,Math.min(1,1.5-Math.abs(4*strength-offset))));
      ctx.fillStyle=`rgba(${channel(3)},${channel(2)},${channel(1)},${heatmapView==='heatmap'?1:0.15+0.65*strength})`;
      ctx.fillRect(index%heat.columns*width,Math.floor(index/heat.columns)*height,width,height);
    });
    ctx.strokeStyle='#ffffff22';ctx.lineWidth=0.7;
    for (let i=1;i<heat.columns;i++) {ctx.beginPath();ctx.moveTo(i*width,0);ctx.lineTo(i*width,canvas.height);ctx.stroke();}
    for (let i=1;i<heat.rows;i++) {ctx.beginPath();ctx.moveTo(0,i*height);ctx.lineTo(canvas.width,i*height);ctx.stroke();}
    $('hotspot-ranking').innerHTML=heat.cells.map((item,index)=>({...item,index})).filter(item=>item.seconds>0).sort((a,b)=>b.seconds-a.seconds).slice(0,5).map((item,index)=>`<button type="button" data-cell="${item.index}">#${index+1} · ${heat.estimated?'~':''}${durationLabel(item.seconds)} · ${item.percentage.toFixed(1)}% ↗</button>`).join('') || '<p class="field-help">No usable pet locations in this range yet. Zero detections do not establish that the pet was absent.</p>';
  }
  function drawMaskResults() {
    const local=heatRun?.hotspot_method==='changes';
    const real=heatRun?.kind==='hotspots' && (local || heatRun.provider==='muse');
    $('mask-results').hidden=!real;if(!real)return;
    const frames=heatRun.mask_frames||heatRun.sam_frames||[],total=heatRun.frame_progress?.total||heatRun.sampling?.sample_count||0;
    $('mask-results-title').textContent='Detection masks';
    $('mask-result-count').textContent=local ? `${frames.length}/${total} frames checked · ${frames.filter(frame=>frame.matches.length).length} accepted masks` : `${frames.length} frames with returned masks · ${total} input frames`;
    $('mask-frame-browser').hidden=!frames.length;
    if(!frames.length){
      $('mask-results-status').textContent=['queued','running','stopping'].includes(heatRun.status) ? 'Detection frames will appear here as results arrive.' : heatRun.mask_previews_available ? 'No segmentation masks were returned. This does not establish that the pet was absent.' : 'This older run did not save mask previews. Rebuild from cached inputs or run a new analysis.';
      return;
    }
    const index=$('mask-follow').checked ? frames.length-1 : Math.max(0,frames.findIndex(frame=>`${frame.window_id}:${frame.frame_index}`===maskFrameKey));
    const frame=frames[index];maskFrameKey=`${frame.window_id}:${frame.frame_index}`;
    $('mask-frame-slider').max=frames.length-1;$('mask-frame-slider').value=index;
    $('mask-previous').disabled=index===0;$('mask-next').disabled=index===frames.length-1;
    $('mask-frame-view').style.aspectRatio=`${selected.width}/${selected.height}`;
    const sourceUrl=`${selected.frame_url}?t=${Math.min(frame.source_seconds,selected.duration_seconds-0.001)}`;
    if($('mask-source-frame').getAttribute('src')!==sourceUrl)$('mask-source-frame').src=sourceUrl;
    if($('mask-overlay-frame').getAttribute('src')!==frame.overlay_url)$('mask-overlay-frame').src=frame.overlay_url;
    $('mask-results-status').textContent=(local ? 'Change detection · Accepted foreground regions over the original frame. Foreground changes are not verified pet sightings. Frames without an accepted region have no coloured overlay.' : 'SAM · Returned segmentation masks over the original recording frame.')+(['error','stopped','interrupted'].includes(heatRun.status) ? ' Partial previews are retained; incomplete processing does not add heatmap coverage.' : ' Previews update as frames are processed.');
    const matches=frame.matches||[];
    const assessment=local ? matches.length?'Accepted foreground mask':'No accepted foreground region · Not used in the heatmap' : `${matches.length} ${matches.length===1?'matching object':'matching objects'}${matches.length>1?' · Ambiguous frame excluded from the heatmap.':''}`;
    $('mask-frame-info').textContent=`Sample ${frame.sample_number}/${total} · Source time ${frame.source_seconds.toFixed(2)}s · ${assessment}`;
    $('mask-object-legend').innerHTML=matches.map(object=>`<span><i style="background:${escape(object.color)}"></i>${local?'Foreground':`Object ${escape(object.id)}`}</span>`).join('');
  }
  function showCell(index,keyboard=false) {
    if (!heat) return;
    cell=index;
    const marker=$('heatmap-cell');marker.hidden=false;
    marker.style.cssText=`left:${index%heat.columns*100/heat.columns}%;top:${Math.floor(index/heat.columns)*100/heat.rows}%;width:${100/heat.columns}%;height:${100/heat.rows}%`;
    const item=heat.cells[index],message=`${heat.estimated?'~':''}${item.seconds.toFixed(1)}s ${heat.occupancy_mode==='footprint'?'occupied':'here'} · ${item.percentage===null?'No seen time yet':`${item.percentage.toFixed(1)}% of ${heat.estimated?'estimated ':''}${heatRun?.hotspot_method==='changes'?'foreground':'seen'} time`}${item.first_seen!==null ? ` · ${heat.estimated?'Sample at':'First visit'} ${clock(item.first_seen)}. Click to play.` : ''}`;
    $('heatmap-tooltip').textContent=message;$('heatmap-tooltip').hidden=false;
    if (keyboard) $('heatmap-keyboard').textContent=`Column ${index%heat.columns+1}, row ${Math.floor(index/heat.columns)+1}. ${message}`;
  }
  function playCell() {
    const time=heat?.cells[cell]?.first_seen;
    if (time!==null && time!==undefined)showHotspotMoment(time);
  }
  function showHotspotMoment(time) {
    const video=$('hotspot-review-video');
    if(video.getAttribute('src')!==selected.url)video.src=selected.url;
    $('hotspot-review').hidden=false;video.currentTime=time;
    video.play().catch(()=>{});$('hotspot-review').scrollIntoView({behavior:'smooth',block:'start'});
  }
  async function refresh(loadRuns=true) {
    const config=await api('/api/offline');sources=config.sources;
    const sourceId=selected?.id||$('recording').value;
    $('recording').innerHTML=sources.map(source=>`<option value="${source.id}">${escape(source.name)}</option>`).join('');
    $('recording').value=sources.some(source=>source.id===sourceId) ? sourceId : sources[0]?.id||'';
    if(!loadRuns)return config;
    const current=config.runs.filter(run=>run.source_id===$('recording').value);
    // Keep local HTTP requests bounded even with a large saved history.
    const results=[];
    for(const entry of current){const run=await api(`/api/offline/${entry.id}`);runs.set(run.id,run);results.push(run);}
    const activeMeta=config.runs.find(run=>['queued','running','stopping'].includes(run.status));
    active=activeMeta ? runs.get(activeMeta.id)||await api(`/api/offline/${activeMeta.id}`) : results.at(0)||null;
    return config;
  }
  async function selectRecording() {
    revision++;
    selected=sources.find(source=>source.id===$('recording').value)||null;
    if (!selected) {error('Load a saved recording to begin.');return;}
    error();heatRun=heat=null;
    $('hotspot-review-video').pause();$('hotspot-review-video').removeAttribute('src');$('hotspot-review').hidden=true;
    $('source-detail').textContent=`${clock(selected.duration_seconds)} · ${selected.width}×${selected.height}`;
    const credit=selected.credit;
    $('source-credit').hidden=!credit;
    $('source-credit').innerHTML=credit ? `Footage: ${escape(credit.creator)} · ${escape(credit.license)} · <a href="${escape(credit.source_url)}" target="_blank" rel="noopener">Source</a> · <a href="${escape(credit.license_url)}" target="_blank" rel="noopener">License</a>. Resized working copy; no endorsement implied.` : '';
    $('range-start').value='00:00';$('range-end').value=clock(Math.min(60,selected.duration_seconds));
    // Preserve fractional endings so a short source is not silently truncated.
    if (selected.duration_seconds<60) $('range-end').value=selected.duration_seconds.toFixed(6);
    $('heatmap-image').src=selected.frame_url;
    $('pet-prompt').value=selected.id==='cat_momi' ? 'orange tabby cat' : '';
    $('heatmap').style.maxWidth=`${Math.min(900,600*selected.width/selected.height)}px`;
    try {localStorage.setItem('pawception.offline.source',selected.id);} catch {}
    await refresh();render();plan();
  }
  async function start(kind) {
    setWorkspace(kind);
    if(kind==='hotspots')setHotspotView('2d');
    error();
    try {
      const value={...range(),source_id:selected.id,kind,provider:$('provider').value,
        events:drafts,hotspot_method:$('hotspot-method').value,pet_prompt:$('pet-prompt').value,max_frames:Number($('max-frames').value),
        change_long_edge:Number($('change-long-edge').value),
        window_seconds:Number($('event-window').value),
        overlap_seconds:Number($('event-overlap').value),merge_gap_seconds:Number($('event-gap').value),
        model:$('model').value,audio_model:$('audio-model').value,reasoning_effort:$('reasoning').value};
      loading=true;render();$('job-title').textContent='Preparing analysis…';$('job-status').textContent='Checking the recording and the selected time range.';
      $('job-eta').textContent='Estimated completion: calculating…';$('job-eta').hidden=false;
      $('job-progress').removeAttribute('value');$('sam-progress-note').hidden=true;
      const result=await api('/api/offline/runs',value);runs.set(result.id,result);active=result;
      viewedJobId=result.id;
      if(kind==='events')showSearch(result.id);
      if (kind==='hotspots') chooseHeatmap(result);
    } catch(e) {error(e.message);} finally {loading=false;render();}
  }
  const descriptionSuggestions=new DescriptionSuggestions({onChange:next=>{
    const choosing=next.phase==='choosing';
    $('recording-event-editor').hidden=choosing;
    $('save-recording-event').hidden=choosing;
    $('recording-event-suggestions').hidden=!choosing;
    $('suggest-recording-description').disabled=next.phase==='loading';
    $('suggest-recording-description').textContent=next.phase==='loading' ? 'Polishing prompt…' : 'Prompt Polish';
    $('recording-suggestion-status').textContent=next.message;
    $('recording-suggestion-status').hidden=!next.message;
    $('recording-suggestion-options').innerHTML=next.options.map((option,index)=>`<button type="button" class="suggestion-option" data-suggestion="${index}"><strong>${escape(option.label)}</strong><span>${escape(option.description)}</span><small>Use and edit ↗</small></button>`).join('');
    if(choosing)$('recording-suggestion-options').querySelector('button')?.focus();
  }});
  $('suggest-recording-description').addEventListener('click',()=>{
    descriptionSuggestions.generate({name:$('recording-event-name').value,description:$('recording-event-description').value,
      reasoning_effort:$('suggestion-reasoning').value},$('provider').value);
  });
  $('recording-suggestion-options').addEventListener('click',event=>{
    const button=event.target.closest('[data-suggestion]');if(!button)return;
    const description=descriptionSuggestions.choose(Number(button.dataset.suggestion));
    if(description===null)return;
    $('recording-event-description').value=description;$('recording-event-description').focus();
  });
  $('reject-recording-suggestions').addEventListener('click',()=>{
    descriptionSuggestions.reset('Your wording is unchanged. You can keep editing.');
    $('recording-event-description').focus();
  });
  for(const id of ['recording-event-name','recording-event-description'])$(id).addEventListener('input',()=>descriptionSuggestions.reset());
  $('recording-event-dialog').addEventListener('close',()=>descriptionSuggestions.reset());
  $('search-form').addEventListener('submit',event=>{event.preventDefault();start('events');});
  $('add-recording-event').addEventListener('click',()=>openEditor());
  $('close-recording-event').addEventListener('click',()=>$('recording-event-dialog').close());
  $('recording-event-form').addEventListener('submit',event=>{
    event.preventDefault();const value={name:$('recording-event-name').value,description:$('recording-event-description').value,input_mode:$('recording-event-mode').value};
    if(editing===null)drafts.push(value);else drafts[editing]=value;
    $('recording-event-dialog').close();saveDrafts();
  });
  $('event-drafts').addEventListener('click',event=>{
    const edit=event.target.closest('[data-edit]'),remove=event.target.closest('[data-remove]');
    if(edit)openEditor(Number(edit.dataset.edit));
    if(remove){const index=Number(remove.dataset.remove);removed.push({id:crypto.randomUUID(),event:drafts[index]});drafts.splice(index,1);saveDrafts();}
  });
  $('trash-events').addEventListener('click',event=>{
    const restore=event.target.closest('[data-restore]'),remove=event.target.closest('[data-delete]');
    if(restore && drafts.length<20){const record=removed.find(record=>record.id===restore.dataset.restore);if(record){drafts.push(record.event);removed=OfflineTools.deleteTrash(removed,record.id);saveDrafts();}}
    if(remove && confirm('Delete this event definition forever? Past search results and recordings will remain.')){removed=OfflineTools.deleteTrash(removed,remove.dataset.delete);saveDrafts();}
  });
  $('empty-event-trash').addEventListener('click',()=>{if(confirm('Empty the event trash? Definitions cannot be restored. Past search results and recordings will remain.')){removed=[];saveDrafts();}});
  $('hotspots-form').addEventListener('submit',event=>{event.preventDefault();start('hotspots');});
  $('recording').addEventListener('change',()=>selectRecording().catch(e=>error(e.message)));
  $('whole-recording').addEventListener('click',()=>{if(selected){$('range-start').value='0';$('range-end').value=String(selected.duration_seconds);plan();}});
  $('heatmap-run').addEventListener('change',()=>{const run=runs.get($('heatmap-run').value);if(run){viewedJobId=run.id;chooseHeatmap(run);setWorkspace('hotspots');}});
  $('search-run').addEventListener('change',()=>showSearch($('search-run').value));
  $('heatmap-background').addEventListener('click',()=>{
    const video=$('hotspot-review').hidden?review.video:$('hotspot-review-video');
    if(selected) $('heatmap-image').src=`${selected.frame_url}?t=${Math.min(video.currentTime,selected.duration_seconds-0.001)}`;
  });
  $('heatmap-views').addEventListener('click',event=>{
    const button=event.target.closest('button[data-view]');if(!button)return;
    heatmapView=button.dataset.view;
    heatmapKeyboard=false;$('heatmap-keyboard').textContent='';
    try {localStorage.setItem('pawception.offline.heatmap-view',heatmapView);} catch {}
    drawHeatmap();
  });
  function selectMaskFrame(index) {
    const frames=heatRun?.mask_frames||heatRun?.sam_frames||[];if(!frames.length)return;
    const frame=frames[Math.max(0,Math.min(frames.length-1,index))];
    $('mask-follow').checked=false;maskFrameKey=`${frame.window_id}:${frame.frame_index}`;drawMaskResults();
  }
  $('mask-frame-slider').addEventListener('input',()=>selectMaskFrame(Number($('mask-frame-slider').value)));
  $('mask-previous').addEventListener('click',()=>selectMaskFrame(Number($('mask-frame-slider').value)-1));
  $('mask-next').addEventListener('click',()=>selectMaskFrame(Number($('mask-frame-slider').value)+1));
  $('mask-follow').addEventListener('change',drawMaskResults);
  $('mask-review-moment').addEventListener('click',()=>{
    const frame=(heatRun?.mask_frames||heatRun?.sam_frames||[]).find(frame=>`${frame.window_id}:${frame.frame_index}`===maskFrameKey);
    if(frame)showHotspotMoment(frame.source_seconds);
  });
  $('heat-filter').addEventListener('submit',async event=>{
    event.preventDefault();error();
    try {
      const start=parseTime($('heat-start').value),end=parseTime($('heat-end').value);
      if(start<heatRun.start_seconds || end>heatRun.end_seconds || end<=start)throw new Error('Choose a filter inside this analysis’s time range.');
      heatFilter={start,end};await refreshHeatmap();
    } catch(e){error(e.message);}
  });
  $('stop-job').addEventListener('click',async()=>{try{active=await api(`/api/offline/${active.id}/stop`,{});runs.set(active.id,active);render();}catch(e){error(e.message);}});
  $('recording-file').addEventListener('change',async()=>{
    const file=$('recording-file').files[0];if(!file)return;
    error();loading=true;render();$('job-title').textContent='Loading your recording…';
    try {
      if(file.size>512*1024*1024)throw new Error('Choose a recording up to 512 MB.');
      const response=await fetch('/api/offline/source',{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-Filename':encodeURIComponent(file.name)},body:file});
      const result=await response.json();if(!response.ok)throw new Error(result.error);
      selected=null;await refresh();$('recording').value=result.id;await selectRecording();
    }catch(e){error(e.message);}finally{loading=false;render();$('recording-file').value='';}
  });
  $('load-recording').addEventListener('click',()=>$('recording-file').click());
  $('analysis-tabs').addEventListener('click',event=>{const tab=event.target.closest('[role=tab]');if(tab)setWorkspace(tab.dataset.workspace);});
  $('analysis-tabs').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();setWorkspace(event.key==='Home'?'events':event.key==='End'?'hotspots':workspace==='events'?'hotspots':'events');
    $(`${workspace}-tab`).focus();
  });
  $('heatmap-canvas').addEventListener('pointermove',event=>{if(!heat || heatmapKeyboard && !event.movementX && !event.movementY)return;heatmapKeyboard=false;const rect=event.currentTarget.getBoundingClientRect();showCell(cellAt(event.clientX-rect.left,event.clientY-rect.top,rect.width,rect.height,heat.columns,heat.rows));});
  $('heatmap-canvas').addEventListener('pointerleave',()=>{if(!heatmapKeyboard){$('heatmap-tooltip').hidden=true;$('heatmap-cell').hidden=true;}});
  $('heatmap-canvas').addEventListener('click',playCell);
  $('heatmap-canvas').addEventListener('keydown',event=>{
    if(!heat)return;
    heatmapKeyboard=true;
    const moves={ArrowRight:1,ArrowLeft:-1,ArrowUp:-heat.columns,ArrowDown:heat.columns};
    if(event.key in moves){event.preventDefault();showCell(Math.max(0,Math.min(heat.cells.length-1,cell+moves[event.key])),true);}
    if(event.key==='Enter'){event.preventDefault();playCell();}
    if(event.key==='Home'){event.preventDefault();showCell(0,true);}
    if(event.key==='End'){event.preventDefault();showCell(heat.cells.length-1,true);}
    if(event.key==='Escape')$('heatmap-tooltip').hidden=true;
  });
  $('hotspot-ranking').addEventListener('click',event=>{const button=event.target.closest('[data-cell]');if(button){cell=Number(button.dataset.cell);playCell();}});
  $('search-history').addEventListener('click',event=>{
    const search=event.target.closest('[data-search]');if(search)showSearch(search.dataset.search);
    const window=event.target.closest('[data-review]');
    if(window){review.showContext(null);review.seek(Number(window.dataset.review),true);return;}
    const button=event.target.closest('[data-match]');if(button){const run=runs.get(button.dataset.run),part=run?.detections[Number(button.dataset.match)];if(part){showSearch(run.id);review.showContext(part);review.seek(part.start,true);}}
  });
  for(const id of ['range-start','range-end','event-window','event-overlap','max-frames','audio-model']) $(id).addEventListener('input',plan);
  for(const id of ['change-long-edge','heatmap-cell-size'])$(id).addEventListener('change',()=>{
    plan();error();
    const size=Number($('heatmap-cell-size').value);
    if(!Number.isInteger(size) || size<16 || size>256){error('Choose a heatmap square size from 16 to 256 pixels.');return;}
    try{localStorage.setItem(spatialKey,JSON.stringify({change_long_edge:Number($('change-long-edge').value),cell_size:size}));}catch{}
    if(id==='heatmap-cell-size')refreshHeatmap().catch(e=>error(e.message));
  });
  for(const id of ['provider','hotspot-method'])$(id).addEventListener('change',()=>{
    plan();render();review.selected=null;review.hideHover();if(review.state)review.showContext(null);
    if(id==='provider')try{localStorage.setItem(modeKey,$('provider').value);}catch{}
  });
  let spatialPolling=false;
  const hotspotTabs=[...$('hotspot-view-tabs').querySelectorAll('[role=tab]')];
  for(const tab of hotspotTabs){
    tab.addEventListener('click',()=>setHotspotView(tab.dataset.hotspotView));
    tab.addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();
      const index=event.key==='Home'?0:event.key==='End'?hotspotTabs.length-1:(hotspotTabs.indexOf(tab)+(event.key==='ArrowRight'?1:-1)+hotspotTabs.length)%hotspotTabs.length;
      setHotspotView(hotspotTabs[index].dataset.hotspotView);hotspotTabs[index].focus();
    });
  }
  function sendSpatialFilter(){
    $('spatial-viewer').contentWindow?.postMessage({type:'spatial-filter',range:heatFilter?[heatFilter.start,heatFilter.end]:null},location.origin);
  }
  function drawSpatial(){
    const state=heatRun?.spatial||{},busy=['queued','running','stopping'].includes(state.status);
    const local=heatRun?.hotspot_method==='changes';
    const eligible=heatRun?.status==='completed'&&(local?heatRun.provider==='local':heatRun?.provider==='muse');
    $('build-spatial').disabled=!eligible||!state.available||busy;
    $('build-spatial').textContent=state.status==='completed'?'Rebuild 3D map':'Build 3D map';
    $('spatial-note').textContent=!eligible?'Select a completed SAM or change-detection hotspot analysis to reuse its masks for 3D.':!state.available?'Install the optional local MoGe environment and checkpoint first (see README).':
      `One depth prediction on the estimated background image forms the room mesh. Each sample adds time to one voxel at its estimated 3D centre.${local?' Assumes the single moving foreground object is the pet. A resting pet may not be located.':''} Geometry and durations are estimates.`;
    $('spatial-progress').hidden=!state.status||state.status==='idle';
    $('stop-spatial').hidden=!busy;$('stop-spatial').disabled=state.status==='stopping';
    const progress=$('spatial-progress-bar');
    if(state.total){progress.max=state.total;progress.value=state.done||0;}else progress.removeAttribute('value');
    progress.hidden=!busy;
    $('spatial-status').textContent=[state.stage,state.total?`${state.done||0}/${state.total} frames`:null,
      Number.isFinite(state.elapsed_seconds)?`${durationLabel(state.elapsed_seconds)} elapsed`:null,
      state.estimated_remaining_seconds>0?`~${durationLabel(state.estimated_remaining_seconds)} remaining in this stage`:null,
      Number.isFinite(state.fixed_fov)?`Fixed FOV ${state.fixed_fov.toFixed(1)}°`:null].filter(Boolean).join(' · ');
    const viewer=$('spatial-viewer'),url=state.scene_url?`/spatial.html?scene=${encodeURIComponent(state.scene_url)}`:null;
    viewer.hidden=!url;
    $('spatial-background').hidden=!url;
    if(state.scene_url)$('spatial-background-image').src=state.scene_url.replace('scene.json','background.png');
    if(url&&hotspotView==='3d'&&viewer.dataset.scene!==url){viewer.dataset.scene=url;viewer.src=url;}
    else if(viewer.dataset.scene&&(!url||viewer.dataset.scene!==url)){delete viewer.dataset.scene;viewer.src='about:blank';}
    if(url&&hotspotView==='3d')sendSpatialFilter();
  }
  $('build-spatial').addEventListener('click',async()=>{
    if(!heatRun)return;error();const run=heatRun;
    setHotspotView('3d');
    $('build-spatial').disabled=true;
    try{
      run.spatial=await api(`/api/offline/${run.id}/spatial`,{fov:$('spatial-fov').value.trim()?Number($('spatial-fov').value):null,
        voxel_size:Number($('spatial-voxel').value)});
      runs.set(run.id,run);render();if(heatRun?.id===run.id)drawSpatial();
    }catch(e){error(e.message);drawSpatial();}
  });
  $('stop-spatial').addEventListener('click',async()=>{if(!heatRun)return;const run=heatRun;try{run.spatial=await api(`/api/offline/${run.id}/spatial/stop`,{});if(heatRun?.id===run.id)drawSpatial();}catch(e){error(e.message);}});
  window.addEventListener('message',event=>{
    if(event.origin!==location.origin||event.source!==$('spatial-viewer').contentWindow)return;
    if(event.data?.type==='spatial-ready')sendSpatialFilter();
    if(event.data?.type==='spatial-review'&&Number.isFinite(event.data.seconds)&&heatRun&&event.data.seconds>=heatRun.start_seconds&&event.data.seconds<=heatRun.end_seconds)showHotspotMoment(event.data.seconds);
  });
  setInterval(async()=>{
    const run=[...runs.values()].find(run=>['queued','running','stopping'].includes(run.spatial?.status));
    if(spatialPolling||!run)return;spatialPolling=true;
    try{run.spatial=await api(`/api/offline/${run.id}/spatial`);render();if(heatRun?.id===run.id){heatRun.spatial=run.spatial;drawSpatial();}}
    catch(e){error(e.message);}finally{spatialPolling=false;}
  },1000);
  setInterval(async()=>{
    if(polling||!active||!['queued','running','stopping'].includes(active.status))return;
    polling=true;const sourceRevision=revision,heatVersion=heatRevision;
    try{
      const query=heatRun?.id===active.id ? heatmapQuery() : '';
      const next=await api(`/api/offline/${active.id}${query}`);runs.set(next.id,next);active=next;
      if(sourceRevision===revision){if(heatRun?.id===next.id && heatVersion===heatRevision){heatRun=next;heat=next.heatmap;drawHeatmap();}render();}
    }catch(e){error(e.message);}finally{polling=false;}
  },1500);
  (async()=>{
    setHotspotView(hotspotView);
    setWorkspace(workspace);
    const config=await refresh(false);
    for(const id of ['model','audio-model']) $(id).innerHTML=Object.entries(config.models).map(([key,name])=>`<option value="${key}">${escape(name)}</option>`).join('');
    $('model').value='muse-spark-1.3-contributor';$('audio-model').value='muse-spark-1.2-contributor';
    try {const saved=localStorage.getItem('pawception.offline.source');if(sources.some(source=>source.id===saved))$('recording').value=saved;} catch {}
    renderDrafts();await selectRecording();
  })().catch(e=>error(e.message));
})();
