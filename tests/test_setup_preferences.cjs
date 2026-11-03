const test = require('node:test');
const assert = require('node:assert/strict');
const preferences = require('../ui/monitor/monitor.js');

function memoryStorage() {
  const values = new Map();
  return {getItem:key=>values.get(key) ?? null,setItem:(key,value)=>values.set(key,value)};
}

test('audio choices, resolved Auto events, and microphone opt-in survive reload and undo',()=>{
  const storage=memoryStorage();
  const setup=preferences.defaults();
  assert.equal(setup.capture_audio,false);
  assert.equal(setup.audio_model,'muse-spark-1.2-contributor');
  Object.assign(setup,{capture_audio:true,audio_model:'muse-spark-1.2',audio_reasoning_effort:'low'});
  setup.events.push({name:'Barking',description:'A dog audibly barking',threshold_seconds:3,palette_index:1,
    input_mode:'auto',resolved_mode:'audio'});
  preferences.save(storage,setup);
  assert.deepEqual(preferences.load(storage),setup);
  const removed=preferences.removeEvent(setup,1,'audio',1);
  assert.deepEqual(preferences.restoreEvent(removed,'audio'),setup);
  assert.equal(preferences.normalize({...setup,events:[{...setup.events[1],resolved_mode:undefined}]}),null);
});

test('older events stay visual and older settings get audio defaults without microphone permission',()=>{
  const setup=preferences.defaults();
  delete setup.audio_model;delete setup.audio_reasoning_effort;delete setup.capture_audio;
  const restored=preferences.normalize(setup);
  assert.equal(restored.capture_audio,false);
  assert.equal(restored.audio_reasoning_effort,'medium');
  assert.equal(restored.events[0].input_mode,undefined);
});

test('every Spark choice is saved and max reasoning remains exclusive to standard 1.3',()=>{
  for (const model of ['muse-spark-1.3','muse-spark-1.3-contributor','muse-spark-1.2','muse-spark-1.2-contributor','muse-spark-1.1']) {
    const setup=preferences.normalize({...preferences.defaults(),model,audio_model:model,audio_reasoning_effort:'max'});
    assert.equal(setup.model,model);
    assert.equal(setup.audio_model,model);
    assert.equal(setup.audio_reasoning_effort,model==='muse-spark-1.3'?'max':'medium');
  }
});

test('legacy SAM preferences are disabled for live monitoring on reload', () => {
  const storage = memoryStorage();
  const setup = preferences.defaults();
  assert.equal(setup.sam_snapshots,false);
  setup.sam_snapshots = true;
  preferences.save(storage,setup);
  assert.equal(preferences.load(storage).sam_snapshots,false);
  delete setup.sam_snapshots;
  assert.equal(preferences.normalize(setup).sam_snapshots,false);
});

test('deleting one trash entry preserves active events and other restorable entries across reload',()=>{
  const storage=memoryStorage();let setup=preferences.defaults();
  setup.events.push({name:'Barking',description:'Repeated barking',threshold_seconds:2,palette_index:1});
  setup=preferences.removeEvent(setup,1,'bark',1);
  setup=preferences.removeEvent(setup,0,'chew',2);
  setup.events.push({name:'Drinking',description:'Drinking water',threshold_seconds:2,palette_index:2});
  const active=JSON.parse(JSON.stringify(setup.events));
  const next=preferences.deleteRemoved(setup,'bark');preferences.save(storage,next);
  assert.deepEqual(preferences.load(storage).events,active);
  assert.deepEqual(preferences.load(storage).removed_events.map(record=>record.id),['chew']);
  assert.equal(preferences.restoreEvent(next,'bark'),null);
  assert.equal(preferences.restoreEvent(next,'chew').events.length,2);
});

test('emptying trash leaves active definitions intact',()=>{
  const setup=preferences.defaults();setup.removed_events=[{id:'old',removed_at:1,event:{...setup.events[0]}}];
  const next=preferences.emptyTrash(setup);
  assert.deepEqual(next.events,setup.events);assert.deepEqual(next.removed_events,[]);
});

test('events, colours, thresholds and developer settings survive a reload', () => {
  const storage = memoryStorage();
  const setup = preferences.defaults();
  setup.provider = 'muse';
  setup.window_seconds = 30;
  setup.overlap_seconds = 5;
  setup.model = 'muse-spark-1.3';
  setup.mock_delay_seconds = 12;
  setup.merge_gap_seconds = 0.7;
  setup.events.push({name:'Furniture being moved',description:'Furniture being moved by pets',threshold_seconds:7,palette_index:4});
  assert.deepEqual(preferences.save(storage,setup),setup);
  assert.deepEqual(preferences.load(storage),setup);
});

test('deleting every event stays deleted instead of restoring the default event', () => {
  const storage = memoryStorage();
  preferences.save(storage,{...preferences.defaults(),events:[]});
  assert.deepEqual(preferences.load(storage).events,[]);
});

test('a live event snapshot is saved as a fresh setup definition', () => {
  const storage = memoryStorage();
  const setup = preferences.defaults();
  setup.events[0] = {...setup.events[0],id:'event_1',active_from:42,analyzed_windows:3};
  preferences.save(storage,setup);
  const event=preferences.load(storage).events[0];
  assert.equal(event.name,'Chewing the furniture');
  assert.equal(event.active_from,undefined);
  assert.equal(event.id,undefined);
});

test('incomplete numeric edits retain the previous valid settings', () => {
  const previous={...preferences.defaults(),window_seconds:30,mock_delay_seconds:8};
  const result=preferences.normalize({...previous,window_seconds:NaN,overlap_seconds:50,mock_delay_seconds:0},previous);
  assert.equal(result.window_seconds,30);
  assert.equal(result.overlap_seconds,0);
  assert.equal(result.mock_delay_seconds,8);
});

test('gap tolerance defaults for old setups and preserves explicit zero',()=>{
  const setup=preferences.defaults();
  delete setup.merge_gap_seconds;
  assert.equal(preferences.normalize(setup).merge_gap_seconds,1);
  assert.equal(preferences.normalize({...setup,merge_gap_seconds:0}).merge_gap_seconds,0);
  assert.equal(preferences.normalize({...setup,merge_gap_seconds:NaN},{...preferences.defaults(),merge_gap_seconds:0.7}).merge_gap_seconds,0.7);
});

test('Safety watch defaults on and preserves a saved off preference',()=>{
  const storage=memoryStorage();
  const setup=preferences.defaults();
  assert.equal(setup.safety_watch,true);
  delete setup.safety_watch;
  assert.equal(preferences.normalize(setup).safety_watch,true);
  preferences.save(storage,{...setup,safety_watch:false});
  assert.equal(preferences.load(storage).safety_watch,false);
});

test('Safety guidance and exclusions persist, migrate old settings and restore defaults',()=>{
  const storage=memoryStorage();
  const setup=preferences.defaults();
  delete setup.safety_prompt; delete setup.safety_exclusions;
  assert.equal(preferences.normalize(setup).safety_prompt,null);
  assert.equal(preferences.normalize(setup).safety_exclusions,'');
  const edited={...setup,safety_prompt:'Watch for exposed staples.',safety_exclusions:'Ignore the training cushion.'};
  preferences.save(storage,edited);
  assert.equal(preferences.load(storage).safety_prompt,edited.safety_prompt);
  assert.equal(preferences.load(storage).safety_exclusions,edited.safety_exclusions);
  const restored=preferences.save(storage,{...edited,safety_prompt:null,safety_exclusions:''});
  assert.equal(restored.safety_prompt,null);
  assert.equal(restored.safety_exclusions,'');
  assert.equal(preferences.normalize({...edited,safety_prompt:'x'.repeat(6001)},edited).safety_prompt,edited.safety_prompt);
});

test('file selection remembers its name without persisting file bytes, handles, or blob URLs', () => {
  const storage=memoryStorage();
  preferences.save(storage,{...preferences.defaults(),source:{kind:'file',name:'My pet.mp4',url:'blob:private',file:'video bytes'},api_key:'secret',session_id:'old-session'});
  assert.deepEqual(preferences.load(storage).source,{kind:'file',name:'My pet.mp4'});
  const raw=storage.getItem(preferences.key);
  for (const sensitive of ['private','video bytes','secret','old-session']) assert.ok(!raw.includes(sensitive));
});

test('corrupt or unavailable browser storage does not crash setup', () => {
  assert.equal(preferences.load({getItem:()=>'{broken'}),null);
  assert.equal(preferences.load({getItem:()=>JSON.stringify({version:99,events:[]})}),null);
  assert.equal(preferences.load(undefined),null);
  assert.equal(preferences.save({setItem:()=>{throw new Error('quota');}},preferences.defaults()),null);
});

test('malformed events are rejected and duplicate palette slots are normalized', () => {
  assert.equal(preferences.normalize({...preferences.defaults(),events:[{name:'',description:'x',threshold_seconds:10}]}),null);
  const setup=preferences.defaults();
  setup.events.push({...setup.events[0]});
  const restored=preferences.normalize(setup);
  assert.equal(new Set(restored.events.map(event=>event.palette_index)).size,2);
});

test('older saved setups load with an empty removed list', () => {
  const setup=preferences.defaults();
  delete setup.removed_events;
  assert.deepEqual(preferences.normalize(setup).removed_events,[]);
});

test('webcam selection survives reload without saving media or device permissions', () => {
  const storage=memoryStorage();
  preferences.save(storage,{...preferences.defaults(),source:{kind:'webcam',stream:'private',deviceId:'private'}});
  assert.deepEqual(preferences.load(storage).source,{kind:'webcam'});
  assert.ok(!storage.getItem(preferences.key).includes('private'));
});

test('removal and restoration survive reload with the complete event definition', () => {
  const storage=memoryStorage();
  const setup=preferences.defaults();
  const removed=preferences.removeEvent(setup,0,'removal-1',1234);
  assert.equal(setup.events.length,1);
  assert.deepEqual(removed.events,[]);
  assert.deepEqual(removed.removed_events,[{id:'removal-1',removed_at:1234,event:setup.events[0]}]);
  preferences.save(storage,removed);
  const restored=preferences.restoreEvent(preferences.load(storage),'removal-1');
  assert.deepEqual(restored,setup);
  preferences.save(storage,restored);
  assert.deepEqual(preferences.load(storage),setup);
  assert.equal(preferences.restoreEvent(restored,'removal-1'),null);
});

test('restoring into an occupied colour preserves active colours and selects a free slot', () => {
  let setup=preferences.removeEvent(preferences.defaults(),0,'old',1);
  const newer={name:'New event',description:'Another event',threshold_seconds:3,palette_index:0};
  setup.events.push(newer);
  const restored=preferences.restoreEvent(setup,'old');
  assert.deepEqual(restored.events[0],newer);
  assert.equal(restored.events[1].palette_index,1);
  assert.equal(restored.events[1].threshold_seconds,5);
});

test('restoring at the active event limit keeps the removed event available', () => {
  const setup=preferences.removeEvent(preferences.defaults(),0,'old',1);
  setup.events=Array.from({length:20},(_,palette_index)=>({name:`Event ${palette_index}`,description:'Watch this',threshold_seconds:10,palette_index}));
  assert.equal(preferences.restoreEvent(setup,'old'),null);
  assert.equal(setup.removed_events.length,1);
});

test('undo targets the selected removal when several events were removed', () => {
  const setup=preferences.defaults();
  setup.events.push({name:'Second event',description:'Watch something else',threshold_seconds:5,palette_index:2});
  const first=preferences.removeEvent(setup,0,'first',1);
  const second=preferences.removeEvent(first,0,'second',2);
  const restored=preferences.restoreEvent(second,'first');
  assert.deepEqual(restored.events,[setup.events[0]]);
  assert.equal(restored.removed_events[0].id,'second');
  assert.equal(preferences.removeEvent(setup,99,'invalid',3),null);
});


test('independent reasoning settings persist and older setups keep defaults',()=>{
  const storage=memoryStorage();
  const setup=preferences.defaults();
  assert.equal(setup.video_reasoning_effort,'medium');
  assert.equal(setup.suggestion_reasoning_effort,'minimal');
  delete setup.video_reasoning_effort; delete setup.suggestion_reasoning_effort;
  assert.equal(preferences.normalize(setup).video_reasoning_effort,'medium');
  assert.equal(preferences.normalize(setup).suggestion_reasoning_effort,'minimal');
  preferences.save(storage,{...setup,video_reasoning_effort:'high',suggestion_reasoning_effort:'low'});
  assert.equal(preferences.load(storage).video_reasoning_effort,'high');
  assert.equal(preferences.load(storage).suggestion_reasoning_effort,'low');
});

test('max is available only for standard Spark and downgrades on model switch',()=>{
  const setup={...preferences.defaults(),model:'muse-spark-1.3',video_reasoning_effort:'max'};
  assert.ok(preferences.effortsForModel(setup.model).includes('max'));
  assert.equal(preferences.normalize(setup).video_reasoning_effort,'max');
  assert.equal(preferences.normalize({...setup,model:'muse-spark-1.3-contributor'},setup).video_reasoning_effort,'xhigh');
  assert.equal(preferences.normalize({...setup,suggestion_reasoning_effort:'max'}).suggestion_reasoning_effort,'minimal');
  assert.equal(preferences.normalize({...setup,video_reasoning_effort:'none'}).video_reasoning_effort,'medium');
});
