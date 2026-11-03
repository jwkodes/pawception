const test=require('node:test');
const assert=require('node:assert/strict');
const {frameAt}=require('../ui/monitor/sam-overlay.js');

test('masks respect exact frame intervals, VFR timing and missing results',()=>{
  const frames=[{start:0,end:0.04,file:'a'},{start:0.04,end:0.1,file:'b'},{start:0.2,end:0.25,file:'c'}];
  assert.equal(frameAt(frames,0).file,'a');
  assert.equal(frameAt(frames,0.04).file,'b');
  for(const time of [-1,0.1,0.15,0.25,5]) assert.equal(frameAt(frames,time),null);
  assert.equal(frameAt([],0),null);
  assert.equal(frameAt(frames,12.22-12).file,'c'); // clip-local time after a source offset
});
