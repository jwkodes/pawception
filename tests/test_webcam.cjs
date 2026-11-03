const test = require('node:test');
const assert = require('node:assert/strict');
const {CameraCapture,cameraMediaURL} = require('../ui/monitor/webcam.js');

class Recorder {
  static isTypeSupported(type) { return type.startsWith('video/webm'); }
  constructor(stream,options) { this.mimeType=options.mimeType; this.state='inactive'; this.listeners={}; }
  addEventListener(name,callback) { this.listeners[name]=callback; }
  start(slice) { this.state='recording'; this.slice=slice; }
  stop() { this.state='inactive'; this.listeners.stop?.(); }
}
const tick = () => new Promise(resolve=>setImmediate(resolve));

test('microphone is requested only by explicit opt-in and is recorded with Opus',async()=>{
  const calls=[],audioTrack={stop:()=>{}},videoTrack={stop:()=>{},addEventListener:()=>{}};
  const camera=new CameraCapture({Recorder,mediaDevices:{getUserMedia:async options=>{
    calls.push(options);
    return {getTracks:()=>options.audio?[videoTrack,audioTrack]:[videoTrack],
      getVideoTracks:()=>[videoTrack],getAudioTracks:()=>options.audio?[audioTrack]:[]};
  }}});
  await camera.open();camera.start('silent');
  assert.equal(calls[0].audio,false);
  assert.equal(camera.recorder.mimeType,'video/webm;codecs=vp8');
  camera.stop();
  await camera.open({audio:true});camera.start('sound');
  assert.equal(calls[1].audio,true);
  assert.equal(camera.recorder.mimeType,'video/webm;codecs=vp8,opus');
  camera.stop();
});

test('default uploads call browser fetch with its Window receiver',async context=>{
  context.mock.method(globalThis,'fetch',function(url) {
    assert.equal(this,globalThis);
    return Promise.resolve({url});
  });
  const camera=new CameraCapture({Recorder});
  assert.deepEqual(await camera.send('/camera',{}),{url:'/camera'});
});

function setup(send) {
  let time=0, stopped=0;
  const errors=[], states=[], tracks=[];
  const camera=new CameraCapture({Recorder,clock:()=>time,send,
    mediaDevices:{getUserMedia:async constraints=>{
      assert.equal(constraints.audio,false);
      const track={stop:()=>stopped++,addEventListener:(name,callback)=>{track[name]=callback;}};
      tracks.push(track);
      return {getTracks:()=>[track],getVideoTracks:()=>[track]};
    }},onError:error=>errors.push(error),onState:state=>states.push(state)});
  return {camera,errors,states,tracks,setTime:value=>time=value,stopped:()=>stopped};
}

test('one recorder uploads chunks in order without overlapping requests', async()=>{
  const calls=[], completions=[];
  const env=setup((url,options)=>new Promise(resolve=>{
    calls.push({url,options}); completions.push(()=>resolve({ok:true,json:async()=>({id:'session'})}));
  }));
  await env.camera.open(); env.camera.start('session');
  env.setTime(1000); env.camera.accept(new Blob(['first']));
  env.setTime(2000); env.camera.accept(new Blob(['second']));
  assert.equal(calls.length,1);
  assert.equal(calls[0].options.headers['X-Capture-Sequence'],'0');
  assert.equal(calls[0].options.headers['X-Capture-End'],'1');
  completions[0](); await tick();
  assert.equal(calls.length,2);
  assert.equal(calls[1].options.headers['X-Capture-Sequence'],'1');
  completions[1](); await tick();
  assert.equal(env.states.length,2);
  env.camera.stop();
  assert.equal(env.stopped(),1);
});

test('transport overflow stops instead of corrupting the recording by dropping chunks', async()=>{
  const env=setup(()=>new Promise(()=>{}));
  await env.camera.open(); env.camera.start('session');
  for (let i=1;i<=4;i++) { env.setTime(i*1000); env.camera.accept(new Blob(['chunk'])); }
  assert.equal(env.errors.length,1);
  assert.match(env.errors[0],/cannot keep up/);
  assert.equal(env.camera.active,false);
  assert.equal(env.camera.queue.length,0);
  assert.equal(env.stopped(),1);
});

test('upload failure releases camera and does not retry or accept later chunks', async()=>{
  let calls=0;
  const env=setup(async()=>{calls++;return {ok:false,json:async()=>({error:'Out of order'})};});
  await env.camera.open(); env.camera.start('session');
  env.setTime(1000); env.camera.accept(new Blob(['chunk'])); await tick();
  env.camera.accept(new Blob(['later']));
  assert.equal(calls,1);
  assert.deepEqual(env.errors,['Out of order']);
  assert.equal(env.stopped(),1);
});

test('camera media URLs carry source-time bounds',()=>{
  assert.equal(cameraMediaURL('abc',2,5),'/webcam-media/abc/clip.mp4?start=2.000&end=5.000');
  assert.equal(cameraMediaURL('abc',2,2.1,true),'/webcam-media/abc/frame.jpg?start=2.000&end=2.100');
  assert.equal(cameraMediaURL('abc',2,2.1009,true),'/webcam-media/abc/frame.jpg?start=2.000&end=2.100');
});

test('a late response from a stopped session cannot update or block the next session',async()=>{
  const completions=[];
  const env=setup(()=>new Promise(resolve=>completions.push(resolve)));
  await env.camera.open(); env.camera.start('old');
  env.setTime(1000); env.camera.accept(new Blob(['old']));
  env.camera.stop();
  await env.camera.open(); env.camera.start('new');
  env.setTime(2000); env.camera.accept(new Blob(['new']));
  assert.equal(completions.length,2);
  completions[0]({ok:true,json:async()=>({id:'old'})}); await tick();
  assert.equal(env.states.length,0);
  assert.equal(env.camera.uploading,true);
  completions[1]({ok:true,json:async()=>({id:'new'})}); await tick();
  assert.deepEqual(env.states,[{id:'new'}]);
  env.camera.stop();
});

test('late recorder and track events from an old camera cannot corrupt a new recording',async()=>{
  const calls=[];
  const env=setup(async(url,options)=>{
    calls.push({url,options});
    return {ok:true,json:async()=>({id:'new'})};
  });
  await env.camera.open(); env.camera.start('old');
  const oldRecorder=env.camera.recorder;
  env.camera.stop();
  await env.camera.open(); env.camera.start('new');
  oldRecorder.listeners.dataavailable({data:new Blob(['old final chunk'])});
  oldRecorder.listeners.stop();
  oldRecorder.listeners.error();
  env.tracks[0].ended();
  assert.equal(env.camera.active,true);
  assert.deepEqual(env.errors,[]);
  assert.equal(calls.length,0);
  env.setTime(1000);
  env.camera.recorder.listeners.dataavailable({data:new Blob(['new first chunk'])});
  await tick();
  assert.equal(calls.length,1);
  assert.equal(calls[0].options.headers['X-Capture-Sequence'],'0');
  assert.equal(await calls[0].options.body.text(),'new first chunk');
  env.camera.stop();
});
