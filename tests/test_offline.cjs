const test=require('node:test');
const assert=require('node:assert/strict');
const {parseTime,clock,windowCount,cellAt,normalizeEvents,deleteTrash,visibleRuns,searchOutcome,currentSearch,durationLabel,hotspotWarning,samProgress,localProgress,completionText}=require('../ui/offline/offline.js');
test('time inputs preserve fractional seconds and hour-long recordings',()=>{
  assert.equal(parseTime('1:00:00'),3600);assert.equal(parseTime('25:30.5'),1530.5);
  assert.equal(parseTime('14.881533'),14.881533);assert.equal(clock(3600),'1:00:00');
});
test('invalid or ambiguous time values are rejected',()=>{
  for(const value of ['','-1','1:70','1:60:00','bad','1:2:3:4'])assert.throws(()=>parseTime(value));
});
test('window planning counts tails and overlapping windows without assuming zero overlap',()=>{
  assert.equal(windowCount(0,10,3,1),5);assert.equal(windowCount(0,60,30),2);
  assert.equal(windowCount(10,25,60),1);assert.throws(()=>windowCount(0,10,3,3));
});
test('heatmap pointer mapping handles frame edges and outside coordinates',()=>{
  assert.equal(cellAt(100,100,100,100,2,2),3);assert.equal(cellAt(-1,-1,100,100,2,2),0);
  assert.equal(cellAt(80,20,100,100,2,2),1);
});
test('new recording event definitions default to Auto and persist as a list',()=>{
  const events=normalizeEvents([{name:' Barking ',description:' Loud barking '},{name:'Chewing',description:'Chewing furniture',input_mode:'visual'}]);
  assert.deepEqual(events,[{name:'Barking',description:'Loud barking',input_mode:'auto'},{name:'Chewing',description:'Chewing furniture',input_mode:'visual'}]);
  assert.deepEqual(normalizeEvents(JSON.parse(JSON.stringify(events))),events);
  assert.deepEqual(normalizeEvents([{name:'Bad',description:'Bad',input_mode:'invalid'}]),[]);
});
test('recording trash deletion targets only the chosen removed definition',()=>{
  const removed=[{id:'one',event:{name:'Barking'}},{id:'two',event:{name:'Chewing'}}];
  const result=deleteTrash(removed,'one');
  assert.deepEqual(result,[removed[1]]);assert.equal(removed.length,2);
});
test('saved searches separate modes and recordings, including empty Muse history',()=>{
  const runs=[{id:'mock',source_id:'cat',provider:'mock',kind:'events',created_at:'1'},
    {id:'real',source_id:'cat',provider:'muse',kind:'events',created_at:'2'},
    {id:'other',source_id:'dog',provider:'muse',kind:'events',created_at:'3'}];
  assert.deepEqual(visibleRuns(runs,'cat','muse','events').map(run=>run.id),['real']);
  assert.deepEqual(visibleRuns(runs,'cat','mock','events').map(run=>run.id),['mock']);
  assert.deepEqual(visibleRuns(runs.slice(0,1),'cat','muse','events'),[]);
});
test('SAM histories separate mock from real and local changes are independent of AI mode',()=>{
  const runs=['mock','muse','local'].map(provider=>({id:provider,source_id:'cat',provider,kind:'hotspots',created_at:'1'}));
  assert.deepEqual(visibleRuns(runs,'cat','muse','hotspots','sam').map(run=>run.id),['muse']);
  for(const mode of ['mock','muse'])assert.deepEqual(visibleRuns(runs,'cat',mode,'hotspots','changes').map(run=>run.id),['local']);
});
test('completed no-match searches are explicit; failed checks never imply absence',()=>{
  const complete={status:'completed',windows:[{status:'completed'}],detections:[]};
  assert.equal(searchOutcome(complete),'No matching events detected.');
  assert.equal(searchOutcome({...complete,detections:[{}]}),'1 matching interval detected.');
  assert.match(searchOutcome({...complete,windows:[{status:'partial'}]}),/incomplete or failed checks/);
  assert.doesNotMatch(searchOutcome({...complete,windows:[{status:'error'}]}),/No matching events/);
});
test('the timeline defaults to one latest search and can review one older search',()=>{
  const searches=[{id:'first',windows:[{id:'old-window'}],events:[{id:'old-event'}]},
    {id:'latest',windows:[{id:'new-window'}],events:[{id:'new-event'}]}];
  assert.equal(currentSearch(searches,null),searches[1]);
  assert.equal(currentSearch(searches,'first'),searches[0]);
  assert.equal(currentSearch(searches,'different-mode'),searches[1]);
  assert.equal(currentSearch([],'first'),null);
  assert.equal(searches.length,2);
});
test('hotspot durations retain subsecond evidence instead of displaying zero',()=>{
  assert.equal(durationLabel(0.18),'0.18s');assert.equal(durationLabel(0.06),'0.06s');
  assert.equal(durationLabel(59.82),'59.8s');assert.equal(durationLabel(120),'02:00');
});
test('sparse local foreground results disclose the usable evidence count',()=>{
  const local={hotspot_method:'changes'},heat={position_samples:3,sample_count:1000};
  assert.match(hotspotWarning(local,heat),/3 of 1000/);
  assert.match(hotspotWarning(local,heat),/0.3%/);
  assert.match(hotspotWarning(local,{...heat,position_samples:0}),/0 of 1000/);
  assert.equal(hotspotWarning(local,{...heat,position_samples:100}), '');
  assert.equal(hotspotWarning({hotspot_method:'sam'},heat),'');
  assert.equal(hotspotWarning(local,{position_samples:0,sample_count:null}),'');
});
test('SAM progress uses sampled frames and never rounds unfinished work up to 100%',()=>{
  const run={kind:'hotspots',hotspot_method:'sam',frame_progress:{received:999,total:1000}};
  assert.equal(samProgress(run).percent,99);
  assert.equal(samProgress({...run,frame_progress:{received:1000,total:1000}}).percent,100);
  assert.equal(samProgress({...run,hotspot_method:'changes'}),null);
  assert.equal(samProgress({...run,kind:'events'}),null);
});
test('local progress separates background rows from compared frames',()=>{
  const run={hotspot_method:'changes',status:'running',local_progress:{stage:'building_background',completed:45,total:90}};
  assert.equal(localProgress(run).percent,50);
  assert.equal(localProgress({...run,local_progress:{stage:'comparing_frames',completed:3,total:1000}}).percent,0);
  assert.equal(localProgress({hotspot_method:'changes',status:'completed',sampling:{sample_count:1000}}).percent,100);
});
test('completion time is approximate and unavailable work never gets a made-up ETA',()=>{
  assert.match(completionText({status:'running',estimated_remaining_seconds:20},0),/About 20s remaining/);
  assert.match(completionText({status:'running',estimated_remaining_seconds:null}),/calculating/);
  assert.match(completionText({status:'running',estimated_stage_remaining_seconds:20}),/Background ready in about 20s, then frame comparison/);
  assert.equal(completionText({status:'completed',elapsed_seconds:3.2}),'Finished in 3.2s.');
  assert.doesNotMatch(completionText({status:'stopped',elapsed_seconds:3.2}),/remaining/);
});
