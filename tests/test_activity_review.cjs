const test = require('node:test');
const assert = require('node:assert/strict');
const {capturedTime, visibleActivities, matchingAlert,eventColors,eventStyle,processingLanes,safetyTimelineStatus} = require('../ui/monitor/activity-review.js');

test('review and previews cannot seek beyond captured footage or source duration', () => {
  assert.equal(capturedTime(80, 12, 97), 12);
  assert.equal(capturedTime(100, 120, 97), 97);
  assert.equal(capturedTime(-3, 12, 97), 0);
  assert.equal(capturedTime(NaN, 12, 97), 0);
});

test('future activity is hidden and partial intervals are clipped', () => {
  assert.deepEqual(visibleActivities([{start:5,end:20,event_id:'a'}, {start:30,end:40,event_id:'b'}], 12, 97),
    [{start:5,end:12,event_id:'a'}]);
});

test('an alert belongs only to the matching event and continuous interval', () => {
  const alerts = [{id:'1',event_id:'a',at:15}, {id:'2',event_id:'a',at:55}, {id:'3',event_id:'b',at:15}];
  assert.equal(matchingAlert({event_id:'a',start:5,end:28},alerts).id,'1');
  assert.equal(matchingAlert({event_id:'a',start:45,end:69},alerts).id,'2');
  assert.equal(matchingAlert({event_id:'c',start:5,end:28},alerts),undefined);
});

test('all twenty event types have unique colours and keep their assigned colour', () => {
  const colours=Array.from({length:20},(_,palette_index)=>eventColors({palette_index}).solid);
  assert.equal(new Set(colours).size,20);
  assert.equal(eventStyle({id:'event_1',palette_index:7},0),eventStyle({id:'event_9',palette_index:7},12));
});

test('processing windows use separate lanes when they overlap', () => {
  const windows=[0,1,2,3].map((start,index)=>({id:String(index),start_seconds:start,end_seconds:start+3}));
  const lanes=processingLanes(windows);
  assert.equal(lanes.length,3);
  assert.deepEqual(lanes[0].map(job=>job.id),['0','3']);
  assert.equal(processingLanes([{start_seconds:0,end_seconds:3},{start_seconds:3,end_seconds:6}]).length,1);
});

test('danger lane distinguishes off, waiting, failed checks and no detected hazards',()=>{
  const state={safety_watch:true,hazards:[],windows:[]};
  assert.equal(safetyTimelineStatus(state),'Waiting for first safety result');
  assert.equal(safetyTimelineStatus({...state,safety_watch:false}),'Safety watch off');
  assert.equal(safetyTimelineStatus({...state,windows:[{status:'completed',safety:{enabled:false}}]}),'Waiting for first safety result');
  assert.equal(safetyTimelineStatus({...state,windows:[{status:'error',safety:{enabled:true}}]}),'No valid safety results');
  assert.equal(safetyTimelineStatus({...state,windows:[{status:'completed',safety:{enabled:true}}]}),'No hazards detected · 1 window checked');
  assert.equal(safetyTimelineStatus({...state,windows:[{status:'running',safety:{enabled:true},safety_checked:true}]}),'No hazards detected · 1 window checked');
  assert.equal(safetyTimelineStatus({...state,windows:Array.from({length:4},()=>({status:'completed',safety:{enabled:true}}))}),'No hazards detected · 4 windows checked');
  assert.equal(safetyTimelineStatus({...state,hazards:[{id:'a'},{id:'b'}]}),'2 hazards detected');
});
