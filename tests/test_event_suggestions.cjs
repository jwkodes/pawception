const test = require('node:test');
const assert = require('node:assert/strict');
const DescriptionSuggestions = require('../ui/monitor/event-suggestions.js');
const options=Array.from({length:3},(_,i)=>({label:`Choice ${i}`,description:`Description ${i}`}));
const response=()=>({ok:true,json:async()=>({provider:'mock',suggestions:options})});
const draft={name:'Chewing',description:'Messing with couch'};

test('selecting a suggestion returns editable text and makes no save request',async()=>{
  const calls=[];
  const suggestions=new DescriptionSuggestions({request:async(url,options)=>{calls.push(url);return response();}});
  await suggestions.generate(draft,'mock');
  assert.equal(suggestions.state.phase,'choosing');
  assert.equal(suggestions.state.options.length,3);
  assert.equal(suggestions.choose(1),'Description 1');
  assert.equal(suggestions.state.phase,'editing');
  assert.deepEqual(calls,['/api/monitor/suggestions']);
  assert.equal(draft.description,'Messing with couch');
});

test('rejecting suggestions preserves the draft and returns to editing',async()=>{
  const suggestions=new DescriptionSuggestions({request:async()=>response()});
  await suggestions.generate(draft,'mock');
  suggestions.reset();
  assert.equal(suggestions.state.phase,'editing');
  assert.deepEqual(suggestions.state.options,[]);
  assert.equal(draft.description,'Messing with couch');
});

test('closing or editing a draft discards a late response',async()=>{
  let finish;
  const suggestions=new DescriptionSuggestions({request:()=>new Promise(resolve=>{finish=resolve;})});
  const pending=suggestions.generate(draft,'mock');
  assert.equal(suggestions.state.phase,'loading');
  suggestions.reset();
  finish(response()); await pending;
  assert.equal(suggestions.state.phase,'editing');
  assert.deepEqual(suggestions.state.options,[]);
});

test('failed requests leave editing available and never replace wording',async()=>{
  const suggestions=new DescriptionSuggestions({request:async()=>({ok:false,json:async()=>({error:'Unavailable'})})});
  await suggestions.generate(draft,'muse');
  assert.equal(suggestions.state.phase,'editing');
  assert.equal(suggestions.state.message,'Unavailable');
  assert.equal(suggestions.choose(0),null);
  assert.equal(draft.description,'Messing with couch');
});

test('empty input makes no request; incomplete options cannot be selected',async()=>{
  let calls=0;
  const suggestions=new DescriptionSuggestions({request:async()=>{calls++;return {ok:true,json:async()=>({suggestions:options.slice(0,2)})};}});
  await suggestions.generate({name:' ',description:''},'mock');
  assert.equal(calls,0);
  await suggestions.generate(draft,'mock');
  assert.equal(suggestions.state.phase,'editing');
  assert.equal(suggestions.choose(0),null);
});


test('suggestion request carries its own selected reasoning effort',async()=>{
  let body;
  const suggestions=new DescriptionSuggestions({request:async(url,options)=>{body=JSON.parse(options.body);return response();}});
  await suggestions.generate({...draft,reasoning_effort:'high'},'muse');
  assert.equal(body.reasoning_effort,'high');
});
