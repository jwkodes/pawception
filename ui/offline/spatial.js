/* A small WebGL viewer: cached geometry only, with no model calls or CDN. */
(() => {
  const $=id=>document.getElementById(id), canvas=$('scene'), tip=$('tooltip');
  const gl=canvas.getContext('webgl',{antialias:true,alpha:true});
  let scene,view='both',filter=null,cells=[],mesh,instances,eye,target=[0,0,-2],radius=3,yaw=0,pitch=.12,distance=3,drag=null,hover=null,pointer=null;
  let framePending=false,program,location,ext;
  const sub=(a,b)=>a.map((v,i)=>v-b[i]),dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0);
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const unit=a=>{const n=Math.hypot(...a)||1;return a.map(v=>v/n);};
  const sum=(a,b)=>a.map((v,i)=>v+b[i]),scale=(a,b)=>a.map(v=>v*b);
  const seconds=n=>n<60?`${n.toFixed(1)}s`:`${Math.floor(n/60)}m ${(n%60).toFixed(0)}s`;
  const overlap=(intervals,start,end)=>intervals.reduce((n,[a,b])=>n+Math.max(0,Math.min(end,b)-Math.max(start,a)),0);
  const jet=t=>[3,2,1].map(offset=>Math.max(0,Math.min(1,1.5-Math.abs(4*t-offset))));
  function shader(type,source){const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;}
  function setup(){
    if(!gl)throw Error('This browser cannot display WebGL. Try a browser with graphics acceleration enabled.');
    ext=gl.getExtension('ANGLE_instanced_arrays');
    if(!ext||!gl.getExtension('OES_element_index_uint'))throw Error('This browser does not support the required 3D drawing features.');
    program=gl.createProgram();
    gl.attachShader(program,shader(gl.VERTEX_SHADER,'attribute vec3 position;attribute vec3 colour;attribute vec3 offset;uniform mat4 matrix;uniform float voxelSize;uniform float instanced;varying vec3 rgb;void main(){vec3 p=mix(position,position*voxelSize+offset,instanced);gl_Position=matrix*vec4(p,1.0);rgb=colour;}'));
    gl.attachShader(program,shader(gl.FRAGMENT_SHADER,'precision mediump float;varying vec3 rgb;uniform float alpha;void main(){gl_FragColor=vec4(rgb,alpha);}'));
    gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error('Could not initialize the 3D renderer.');
    gl.useProgram(program);location={};
    for(const name of ['position','colour','offset'])location[name]=gl.getAttribLocation(program,name);
    for(const name of ['matrix','voxelSize','instanced','alpha'])location[name]=gl.getUniformLocation(program,name);
    gl.enable(gl.DEPTH_TEST);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
  }
  function buffer(data,type=gl.ARRAY_BUFFER){const b=gl.createBuffer();gl.bindBuffer(type,b);gl.bufferData(type,data,gl.STATIC_DRAW);return b;}
  function attribute(name,b,divisor=0){gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.enableVertexAttribArray(location[name]);gl.vertexAttribPointer(location[name],3,gl.FLOAT,false,0,0);ext.vertexAttribDivisorANGLE(location[name],divisor);}
  function meshBuffers(){
    const positions=scene.mesh.positions.map((n,i)=>i%3===0?n:-n);
    mesh={position:buffer(new Float32Array(positions)),colour:buffer(new Float32Array(scene.mesh.colours.map(n=>n/255))),
      index:buffer(new Uint32Array(scene.mesh.triangles),gl.ELEMENT_ARRAY_BUFFER),count:scene.mesh.triangles.length};
    const corners=[[-.5,-.5,-.5],[.5,-.5,-.5],[.5,.5,-.5],[-.5,.5,-.5],[-.5,-.5,.5],[.5,-.5,.5],[.5,.5,.5],[-.5,.5,.5]],
      indices=[0,1,2,0,2,3,4,6,5,4,7,6,0,4,5,0,5,1,3,2,6,3,6,7,0,3,7,0,7,4,1,5,6,1,6,2];
    instances={position:buffer(new Float32Array(indices.flatMap(i=>corners[i])))};
    const points=[];for(let i=0;i<positions.length;i+=3)points.push(positions.slice(i,i+3));
    if(!points.length)for(const cell of scene.voxels)points.push(cell.key.map((k,i)=>(k+.5)*scene.voxel_size*(i===0?1:-1)));
    if(points.length){
      const bounds=[0,1,2].map(i=>{const a=points.map(p=>p[i]).sort((a,b)=>a-b);return [a[Math.floor(a.length*.02)],a[Math.min(a.length-1,Math.floor(a.length*.98))]];});
      target=bounds.map(([a,b])=>(a+b)/2);radius=Math.max(.3,Math.hypot(...bounds.map(([a,b])=>b-a))/2);
    }
    scene.orbitTarget=[...target];distance=radius*1.7;
  }
  function filterCells(){
    if(!scene)return;
    const [start,end]=filter||scene.range,seen=overlap(scene.seen,start,end);
    cells=scene.voxels.map(cell=>{
      const time=overlap(cell.intervals,start,end);
      const first=cell.intervals.find(([a,b])=>a<end&&b>start);
      return {...cell,time,percent:seen?100*time/seen:0,review:first?Math.max(start,first[0]):cell.first_seen,
        centre:cell.key.map((k,i)=>(k+.5)*scene.voxel_size*(i===0?1:-1))};
    }).filter(cell=>cell.time>0);
    const maximum=cells.reduce((maximum,cell)=>Math.max(maximum,cell.time),0),positions=[],colours=[],total=cells.length;
    const cutoff=Number($('threshold').value)/100;
    cells=cells.filter(cell=>cell.time+1e-8>=maximum*cutoff);
    for(const cell of cells){positions.push(...cell.centre);colours.push(...jet(Math.sqrt(cell.time/(maximum||1))));}
    if(instances.offset)gl.deleteBuffer(instances.offset);if(instances.colour)gl.deleteBuffer(instances.colour);
    instances.offset=buffer(new Float32Array(positions));instances.colour=buffer(new Float32Array(colours));
    $('summary').textContent=`${cells.length.toLocaleString()}/${total.toLocaleString()} voxels shown · ${seconds(seen)} ${scene.hotspot_method==='changes'?'foreground located':'pet seen'} · Peak ${seconds(maximum)}`;
    $('details').textContent=`Single-image background depth${scene.background_resolution?' · '+scene.background_resolution.join('×')+' mesh grid':''} · Fixed FOV ${scene.fixed_fov.toFixed(1)}° · ${scene.voxel_size.toFixed(2)} predicted-metre voxels · ${scene.stats.mesh_triangles.toLocaleString()} background triangles${scene.mesh_edge_tolerance?' · '+(100*scene.mesh_edge_tolerance).toFixed(0)+'% depth-edge filter':''}. Approximate depth; no registration. Background-image artifacts carry into the mesh. Hidden surfaces remain unknown. ${scene.occupancy_mode==='centre'?'One median pet centre per sample; voxel percentages add up to 100% of located time.':'Older result: full mask occupancy; several voxels count the same moment.'}${scene.hotspot_method==='changes'?' Assumes the single moving foreground object is the pet.':''}`;
    hover=null;tip.hidden=true;requestRender();
  }
  function multiply(a,b){const c=new Float32Array(16);for(let col=0;col<4;col++)for(let row=0;row<4;row++)for(let k=0;k<4;k++)c[col*4+row]+=a[k*4+row]*b[col*4+k];return c;}
  const projectionScale=()=>Math.min(2*scene.intrinsics[1][1],2*scene.intrinsics[0][0]*canvas.width/canvas.height);
  function cameraMatrix(){
    eye=sum(target,[distance*Math.sin(yaw)*Math.cos(pitch),distance*Math.sin(pitch),distance*Math.cos(yaw)*Math.cos(pitch)]);
    const z=unit(sub(eye,target)),x=unit(cross([0,1,0],z)),y=cross(z,x);
    const v=new Float32Array([x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,-dot(x,eye),-dot(y,eye),-dot(z,eye),1]);
    const aspect=canvas.width/canvas.height,f=projectionScale(),near=.01,far=200;
    const p=new Float32Array([f/aspect,0,0,0,0,f,0,0,0,0,(far+near)/(near-far),-1,0,0,2*far*near/(near-far),0]);
    return multiply(p,v);
  }
  function render(){
    framePending=false;if(!scene)return;
    const ratio=Math.min(2,window.devicePixelRatio||1),w=Math.round(canvas.clientWidth*ratio),h=Math.round(canvas.clientHeight*ratio);
    if(!w||!h)return;
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
    gl.viewport(0,0,w,h);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
    gl.uniformMatrix4fv(location.matrix,false,cameraMatrix());
    if(view!=='heat'&&mesh.count){
      attribute('position',mesh.position);attribute('colour',mesh.colour);
      gl.disableVertexAttribArray(location.offset);ext.vertexAttribDivisorANGLE(location.offset,0);gl.vertexAttrib3f(location.offset,0,0,0);
      gl.uniform1f(location.instanced,0);gl.uniform1f(location.alpha,view==='room'?1:.45);
      gl.depthMask(view==='room');gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,mesh.index);gl.drawElements(gl.TRIANGLES,mesh.count,gl.UNSIGNED_INT,0);
    }
    if(view!=='room'&&cells.length){
      gl.depthMask(true);attribute('position',instances.position);attribute('colour',instances.colour,1);attribute('offset',instances.offset,1);
      gl.uniform1f(location.instanced,1);gl.uniform1f(location.voxelSize,scene.voxel_size*.94);gl.uniform1f(location.alpha,.85);
      ext.drawArraysInstancedANGLE(gl.TRIANGLES,0,36,cells.length);
    }
    gl.depthMask(true);if(pointer&&!drag)pick(pointer);
  }
  function requestRender(){if(!framePending){framePending=true;requestAnimationFrame(render);}}
  function pick(event){
    if(view==='room'){tip.hidden=true;hover=null;return;}
    const rect=canvas.getBoundingClientRect(),nx=2*(event.clientX-rect.left)/rect.width-1,ny=1-2*(event.clientY-rect.top)/rect.height;
    const forward=unit(sub(target,eye)),right=unit(cross(forward,[0,1,0])),up=cross(right,forward),tan=1/projectionScale();
    const ray=unit(sum(forward,sum(scale(right,nx*tan*rect.width/rect.height),scale(up,ny*tan))));
    const half=scene.voxel_size*.47;let nearest=Infinity;hover=null;
    for(const cell of cells){let low=0,high=Infinity;for(let i=0;i<3;i++){
      if(Math.abs(ray[i])<1e-8){if(Math.abs(eye[i]-cell.centre[i])>half){high=-1;break;}continue;}
      const a=(cell.centre[i]-half-eye[i])/ray[i],b=(cell.centre[i]+half-eye[i])/ray[i];low=Math.max(low,Math.min(a,b));high=Math.min(high,Math.max(a,b));
    }if(high>=low&&low<nearest){nearest=low;hover=cell;}}
    tip.hidden=!hover;if(!hover)return;
    tip.textContent=`${seconds(hover.time)} · ${hover.percent.toFixed(1)}% of seen time\nClick to review this location`;
    tip.style.left=`${Math.min(rect.width-225,Math.max(8,event.clientX-rect.left+15))}px`;
    tip.style.top=`${Math.max(8,event.clientY-rect.top-70)}px`;
  }
  canvas.addEventListener('pointerdown',event=>{if(scene)pick(event);drag={x:event.clientX,y:event.clientY,moved:0,pan:event.shiftKey||event.button===2};canvas.setPointerCapture(event.pointerId);});
  canvas.addEventListener('pointermove',event=>{
    pointer={clientX:event.clientX,clientY:event.clientY};
    if(drag){const dx=event.clientX-drag.x,dy=event.clientY-drag.y;drag.moved+=Math.hypot(dx,dy);drag.x=event.clientX;drag.y=event.clientY;
      if(drag.pan){const forward=unit(sub(target,eye)),right=unit(cross(forward,[0,1,0])),up=cross(right,forward);target=sum(target,sum(scale(right,-dx*distance/canvas.clientHeight),scale(up,dy*distance/canvas.clientHeight)));}
      else{yaw-=dx*.007;pitch=Math.max(-1.5,Math.min(1.5,pitch+dy*.007));}tip.hidden=true;
    }requestRender();
  });
  canvas.addEventListener('pointerup',event=>{if(drag?.moved<5&&hover)parent.postMessage({type:'spatial-review',seconds:hover.review},locationOrigin());drag=null;canvas.releasePointerCapture(event.pointerId);requestRender();});
  canvas.addEventListener('pointercancel',()=>{drag=null;});canvas.addEventListener('pointerleave',()=>{pointer=null;tip.hidden=true;});
  canvas.addEventListener('contextmenu',event=>event.preventDefault());
  canvas.addEventListener('wheel',event=>{event.preventDefault();distance=Math.max(.1,Math.min(100,distance*Math.exp(event.deltaY*.001)));requestRender();},{passive:false});
  canvas.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','-'].includes(event.key)){event.preventDefault();if(event.key==='ArrowLeft')yaw-=.1;if(event.key==='ArrowRight')yaw+=.1;if(event.key==='ArrowUp')pitch=Math.min(1.5,pitch+.1);if(event.key==='ArrowDown')pitch=Math.max(-1.5,pitch-.1);if(event.key==='+')distance*=.9;if(event.key==='-')distance*=1.1;requestRender();}});
  const locationOrigin=()=>window.location.origin;
  for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>{view=button.dataset.view;for(const b of document.querySelectorAll('[data-view]'))b.setAttribute('aria-pressed',String(b===button));requestRender();});
  $('reset').addEventListener('click',()=>{target=[...scene.orbitTarget];yaw=0;pitch=.12;distance=radius*1.7;requestRender();});
  $('threshold').addEventListener('input',()=>{$('threshold-label').textContent=`${$('threshold').value}%`;filterCells();});
  $('camera').addEventListener('click',()=>{const z=Math.max(.2,-scene.orbitTarget[2]);target=[0,0,-z];yaw=0;pitch=0;distance=z;requestRender();});
  window.addEventListener('message',event=>{if(event.origin!==locationOrigin()||event.source!==parent||event.data?.type!=='spatial-filter')return;const range=event.data.range;if(range===null||Array.isArray(range)&&range.length===2&&range.every(Number.isFinite)&&range[1]>range[0]){filter=range;filterCells();}});
  new ResizeObserver(requestRender).observe(canvas);
  (async()=>{
    setup();const url=new URLSearchParams(window.location.search).get('scene');
    if(!/^\/offline-results\/[a-f0-9]{32}\/spatial\/[a-f0-9]{32}\/scene.json$/.test(url||''))throw Error('Choose a saved 3D analysis.');
    const response=await fetch(url);if(!response.ok)throw Error('The saved 3D scene is unavailable.');scene=await response.json();
    meshBuffers();filterCells();$('loading').hidden=true;parent.postMessage({type:'spatial-ready'},locationOrigin());
  })().catch(error=>{$('loading').textContent=error.message;});
})();
