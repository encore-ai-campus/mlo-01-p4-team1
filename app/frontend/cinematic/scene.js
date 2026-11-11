import * as THREE from 'three';
import {safeFrameDelta} from './timing.mjs';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

const $ = id => document.getElementById(id);
const TAU = Math.PI * 2, DURATION = 32, CHAPTER = 7;
const params = new URLSearchParams(location.search);
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const config = [
  { title:'Spring', name:'봄', label:'THE AWAKENING', line:'봄의 결, 새로운 시작.', description:'잠들었던 풍경이 깨어나고,<br>당신의 하루에 새로운 리듬이 피어납니다.', detail:'BLOSSOM IN MOTION', caption:'A MOMENT OF BLOOM', accent:'#efb6cd', background:'#0d101b', leaf1:'#faadc7', leaf2:'#fff0df', earth:'#345b4f', grass:'#72a77f', water:'#4a9b99', sun:'#ffe8cc', light:'#ffd1d0', rim:'#8bb5ff' },
  { title:'Summer', name:'여름', label:'THE FULLNESS', line:'여름의 빛, 짙어지는 순간.', description:'푸른 생명과 쏟아지는 햇살.<br>모든 순간이 가장 선명하게 빛납니다.', detail:'ALIVE IN THE LIGHT', caption:'A WORLD IN FULL COLOR', accent:'#b7e6a5', background:'#08181d', leaf1:'#399765', leaf2:'#b7e878', earth:'#2e6a4e', grass:'#8ed28a', water:'#37bbb7', sun:'#ffcf72', light:'#ffe8ab', rim:'#60d9d3' },
  { title:'Autumn', name:'가을', label:'THE TRANSFORMATION', line:'가을의 온도, 깊어지는 취향.', description:'바람에 물든 풍경이 춤을 추고,<br>익숙한 일상에 새로운 색이 스며듭니다.', detail:'DANCING WITH THE WIND', caption:'THE ART OF LETTING GO', accent:'#f5b274', background:'#1b1014', leaf1:'#c34a26', leaf2:'#ffc76c', earth:'#735139', grass:'#c59a59', water:'#7d9d9f', sun:'#ffb270', light:'#ffba86', rim:'#b7a1fc' },
  { title:'Winter', name:'겨울', label:'THE STILLNESS', line:'겨울의 고요, 다음을 위한 숨.', description:'눈처럼 차분히 내려앉은 시간.<br>또 다른 시작을 기다리는, 우리만의 계절.', detail:'EVERY END, A BEGINNING', caption:'A QUIET KIND OF WONDER', accent:'#c6deef', background:'#0a1223', leaf1:'#c6d9ec', leaf2:'#edf6ff', earth:'#c7dce5', grass:'#e9f5f6', water:'#8fc8ec', sun:'#e3eeff', light:'#c4dcff', rim:'#879dfa' }
];
let timeline = Math.max(0, Math.min(DURATION, Number(params.get('at')) || 0));
let paused = params.get('paused') === '1';
let lastChapter = -1, brandOn = false, titleTimeout, loaded = false, renderer, composer;
let scene, camera, world, weather, particles, logo, sun, sunHalo, keyLight, rimLight, ambient;
let terrain, terrainVariance, grassMat, leafMat, snowMat, flowerMat, barkMat, riverMat, ringMat, particleMat;
let orbitTrails = [], floatingRocks = [], waterFalls = [], seasonMaterialColors = [];
let last = performance.now(), visualTime = 0, frame = 0, firstFrame = true;
let animationId = 0, disposed = false, environmentTarget;
function notifyParent(type){
  if(window.parent===window)return;
  window.parent.postMessage({type,version:1},new URL(document.baseURI).origin);
}
function enterPlatform(event){
  if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||window.parent===window)return;
  event.preventDefault();
  notifyParent('mlo:cinematic:enter');
}

const pointer = new THREE.Vector2(), smoothPointer = new THREE.Vector2();
const colorA = new THREE.Color(), colorB = new THREE.Color(), mixColor = new THREE.Color();
const leafUniforms = {uTime:{value:0},uWinter:{value:0}};
const snowUniforms = {uTime:{value:0},uWinter:{value:0}};
const grassUniforms = {uTime:{value:0},uWinter:{value:0}};
const flowerUniforms = {uTime:{value:0},uWinter:{value:0}};
let audioContext, audioGain, audioVoices = [], soundOn = false;
let rng = 732411;
function random(){rng=(Math.imul(1664525,rng)+1013904223)>>>0;return rng/4294967296;}
const rand = (a,b) => a+(b-a)*random();
const clamp = (v,a=0,b=1) => Math.min(b,Math.max(a,v));
const smooth = t => {t=clamp(t);return t*t*(3-2*t);};
function blended(a,b,t){return colorA.set(a).lerp(colorB.set(b),t);}
function surface(x,z){const r=Math.sqrt(x*x+z*z);return .16+.11*Math.sin(x*1.6)*Math.cos(z*1.1)+.17*Math.sin(r*.7)+.03*Math.sin(x*5+z*3);}
function patched(material,uniforms,mode){
  material.onBeforeCompile = shader => {
    Object.assign(shader.uniforms,uniforms);
    shader.vertexShader='uniform float uTime; uniform float uWinter;\n'+shader.vertexShader;
    const scale=mode==='leaf'?'mix(1.0,0.012,uWinter)':mode==='snow'?'uWinter':mode==='flower'?'mix(1.0,0.12,uWinter)':'1.0';
    shader.vertexShader=shader.vertexShader.replace('#include <begin_vertex>',`#include <begin_vertex>\ntransformed *= ${scale};\n#ifdef USE_INSTANCING\nfloat phase=instanceMatrix[3].x*2.1+instanceMatrix[3].z*1.7;\ntransformed.x += sin(uTime*1.4+phase)*${mode==='grass'?'0.035':'0.035'}*(1.0-uWinter*.6);\ntransformed.z += cos(uTime*.9+phase)*.018;\n#endif`);
  };
  material.customProgramCacheKey=()=>mode;
  return material;
}
function glowTexture(){
  const c=document.createElement('canvas');c.width=c.height=128;
  const ctx=c.getContext('2d'),g=ctx.createRadialGradient(64,64,0,64,64,64);
  g.addColorStop(0,'rgba(255,255,255,1)');g.addColorStop(.12,'rgba(255,255,255,.75)');g.addColorStop(.35,'rgba(255,255,255,.15)');g.addColorStop(1,'rgba(255,255,255,0)');
  ctx.fillStyle=g;ctx.fillRect(0,0,128,128);return new THREE.CanvasTexture(c);
}
function makeTerrain(){
  const positions=[],colors=[],indices=[],rings=33,segments=144,variance=[];
  for(let r=0;r<=rings;r++)for(let a=0;a<=segments;a++){
    const theta=a/segments*TAU,rad=r/rings*4.5,x=Math.cos(theta)*rad,z=Math.sin(theta)*rad;
    positions.push(x,surface(x,z),z);const v=.75+.25*random();variance.push(v);colors.push(v,v,v);
    if(r<rings&&a<segments){const i=r*(segments+1)+a;indices.push(i,i+1,i+segments+1,i+1,i+segments+2,i+segments+1);}
  }
  const geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));geo.setAttribute('color',new THREE.Float32BufferAttribute(colors,3));geo.setIndex(indices);geo.computeVertexNormals();
  terrain=new THREE.Mesh(geo,new THREE.MeshStandardMaterial({vertexColors:true,roughness:.96,side:THREE.DoubleSide}));terrain.receiveShadow=true;terrainVariance=variance;world.add(terrain);
  const profile=[new THREE.Vector2(.35,-3.0),new THREE.Vector2(1.45,-2.65),new THREE.Vector2(2.4,-2.05),new THREE.Vector2(3.3,-1.2),new THREE.Vector2(4.15,-.55),new THREE.Vector2(4.5,.14)];
  const rockGeo=new THREE.LatheGeometry(profile,88),pos=rockGeo.attributes.position,col=[];
  for(let i=0;i<pos.count;i++){
    const x=pos.getX(i),y=pos.getY(i),z=pos.getZ(i),theta=Math.atan2(z,x),disturbance=1+.035*Math.sin(theta*7+y*3)+.018*Math.cos(theta*13+y*8);
    pos.setX(i,x*disturbance);pos.setZ(i,z*disturbance);
    const shade=.63+rand(0,.22),v=new THREE.Color(y< -1.4?'#28303c':y<-.6?'#49505a':'#6d695d').multiplyScalar(shade);
    col.push(v.r,v.g,v.b);
  }
  rockGeo.setAttribute('color',new THREE.Float32BufferAttribute(col,3));rockGeo.computeVertexNormals();
  const rock=new THREE.Mesh(rockGeo,new THREE.MeshStandardMaterial({vertexColors:true,roughness:.89,metalness:.12}));rock.castShadow=true;rock.receiveShadow=true;world.add(rock);
  for(let j=0;j<3;j++){
    const radius=4.48-j*.18,g=new THREE.TorusGeometry(radius,.013,5,180),m=new THREE.MeshBasicMaterial({color:j===0?'#a5c6ae':'#827b69',transparent:true,opacity:j===0?.35:.13});
    const line=new THREE.Mesh(g,m);line.rotation.x=Math.PI/2;line.position.y=.02-j*.15;world.add(line);
  }
  const rootGeo=[];
  for(let i=0;i<14;i++){
    const a=random()*TAU,rr=rand(1.6,3.9);
    const curve=new THREE.CatmullRomCurve3([new THREE.Vector3(Math.cos(a)*rr,-.8,Math.sin(a)*rr),new THREE.Vector3(Math.cos(a+.2)*rr*.8,-2.1,Math.sin(a+.2)*rr*.8),new THREE.Vector3(Math.cos(a+.4)*rr*.6,-rand(2.7,3.4),Math.sin(a+.4)*rr*.6)]);
    rootGeo.push(new THREE.TubeGeometry(curve,12,rand(.018,.045),5,false));
  }
  const roots=new THREE.Mesh(mergeGeometries(rootGeo),new THREE.MeshStandardMaterial({color:'#696164',roughness:1}));world.add(roots);rootGeo.forEach(g=>g.dispose());
}
function makeTree(){
  const segments=[],tips=[],snowTips=[];
  function taperedPath(points,r0,r1){
    const curve=new THREE.CatmullRomCurve3(points),count=7;
    for(let i=0;i<count;i++){
      const a=curve.getPoint(i/count),b=curve.getPoint((i+1)/count),delta=b.clone().sub(a),radius0=THREE.MathUtils.lerp(r0,r1,i/count),radius1=THREE.MathUtils.lerp(r0,r1,(i+1)/count);
      const g=new THREE.CylinderGeometry(radius1,radius0,delta.length()+.012,7,1);g.applyQuaternion(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0,1,0),delta.normalize()));g.translate((a.x+b.x)/2,(a.y+b.y)/2,(a.z+b.z)/2);segments.push(g);
    }
  }
  const trunk=[];
  for(let i=0;i<=9;i++)trunk.push(new THREE.Vector3(.20*Math.sin(i*.44),.2+i*.49,.15*Math.sin(i*.7)));
  taperedPath(trunk,.30,.055);
  function branch(start,dir,length,radius,depth){
    const end=start.clone().addScaledVector(dir,length),mid=start.clone().lerp(end,.5);mid.y-=length*.06;mid.z+=rand(-.1,.1);
    taperedPath([start,mid,end],radius,radius*.28);
    if(depth===0){tips.push(end);return;}
    const count=depth===2?3:2;
    for(let j=0;j<count;j++){
      const v=dir.clone().add(new THREE.Vector3(rand(-.8,.8),rand(-.12,.58),rand(-.8,.8))).normalize();
      branch(end,v,length*rand(.53,.72),radius*.52,depth-1);
    }
  }
  for(let i=0;i<19;i++){
    const y=1.25+i*.17,a=i*2.39996;
    const start=new THREE.Vector3(.20*Math.sin(y*.9),y,.12*Math.sin(y*1.3));
    const dir=new THREE.Vector3(Math.cos(a),rand(.4,.8),Math.sin(a)).normalize();
    const length=i<6?rand(1.6,2.1):rand(1.2,1.85);
    branch(start,dir,length,.12-i*.0027,2);
  }
  tips.push(new THREE.Vector3(.2,4.8,0));
  barkMat=new THREE.MeshStandardMaterial({color:'#6b5148',roughness:.86});
  const tree=new THREE.Mesh(mergeGeometries(segments),barkMat);tree.castShadow=true;tree.receiveShadow=true;world.add(tree);segments.forEach(g=>g.dispose());
  const count=tips.length*21,geo=new THREE.SphereGeometry(1,7,5);
  leafMat=patched(new THREE.MeshStandardMaterial({color:'#f4c2d5',roughness:.72,metalness:.03,emissive:'#a24268',emissiveIntensity:.06}),leafUniforms,'leaf');
  const leaves=new THREE.InstancedMesh(geo,leafMat,count),dummy=new THREE.Object3D();
  const variants=[];
  let n=0;
  for(const tip of tips){
    for(let i=0;i<21;i++){
      const a=random()*TAU,z=rand(-1,1),r=Math.cbrt(random())*.65,s=Math.sqrt(1-z*z);
      dummy.position.copy(tip).add(new THREE.Vector3(Math.cos(a)*r*s,z*r*.7,Math.sin(a)*r*s));
      dummy.rotation.set(rand(0,Math.PI),rand(0,Math.PI),rand(0,Math.PI));const size=rand(.17,.30);dummy.scale.set(size,size*.23,size*.86);dummy.updateMatrix();leaves.setMatrixAt(n,dummy.matrix);variants.push(random());n++;
    }
    snowTips.push(tip.clone());
  }
  leaves.castShadow=true;leaves.receiveShadow=true;world.add(leaves);seasonMaterialColors.push({mesh:leaves,variants,type:'leaf'});
  snowMat=patched(new THREE.MeshStandardMaterial({color:'#e3eef7',roughness:.8,metalness:.07}),snowUniforms,'snow');
  const snow=new THREE.InstancedMesh(new THREE.SphereGeometry(1,8,5),snowMat,snowTips.length);
  snowTips.forEach((p,i)=>{dummy.position.copy(p).add(new THREE.Vector3(0,.025,0));dummy.rotation.set(0,random()*TAU,0);dummy.scale.set(rand(.16,.31),rand(.045,.09),rand(.17,.28));dummy.updateMatrix();snow.setMatrixAt(i,dummy.matrix);});snow.castShadow=true;world.add(snow);
  // Roots spread over the island, anchoring the tree in the terrain.
  const rootPaths=[];for(let i=0;i<9;i++){const a=i/9*TAU;const end=new THREE.Vector3(Math.cos(a)*rand(.8,1.5),.22,Math.sin(a)*rand(.8,1.4));rootPaths.push(new THREE.TubeGeometry(new THREE.CatmullRomCurve3([new THREE.Vector3(.03,.37,.02),new THREE.Vector3(end.x*.55,.30,end.z*.5),end]),10,.065,6,false));}
  world.add(new THREE.Mesh(mergeGeometries(rootPaths),barkMat));rootPaths.forEach(g=>g.dispose());
}
function makeGroundDetails(){
  const dummy=new THREE.Object3D();
  grassMat=patched(new THREE.MeshStandardMaterial({color:'#86ad83',roughness:.95,side:THREE.DoubleSide}),grassUniforms,'grass');
  const grass=new THREE.InstancedMesh(new THREE.ConeGeometry(.035,.25,3),grassMat,630);
  for(let i=0;i<630;i++){
    const a=random()*TAU,r=Math.sqrt(random())*4.35,x=Math.cos(a)*r,z=Math.sin(a)*r;
    const besideRiver=Math.abs(x-(1.45+.62*Math.sin(z*.8)))<.42;
    dummy.position.set(x,surface(x,z)+.07,z);dummy.rotation.set(rand(-.3,.3),random()*TAU,rand(-.3,.3));dummy.scale.setScalar(besideRiver?.01:rand(.6,1.9));dummy.updateMatrix();grass.setMatrixAt(i,dummy.matrix);
  }
  world.add(grass);
  flowerMat=patched(new THREE.MeshStandardMaterial({color:'#f7d3dc',roughness:.6,emissive:'#ea92ba',emissiveIntensity:.1}),flowerUniforms,'flower');
  const flowers=new THREE.InstancedMesh(new THREE.IcosahedronGeometry(.07,1),flowerMat,220);
  for(let i=0;i<220;i++){const a=random()*TAU,r=rand(1,4.2),x=Math.cos(a)*r,z=Math.sin(a)*r;dummy.position.set(x,surface(x,z)+.1,z);dummy.rotation.set(random(),random(),random());dummy.scale.setScalar(rand(.6,1.6));dummy.updateMatrix();flowers.setMatrixAt(i,dummy.matrix);}world.add(flowers);
  const pebbleMat=new THREE.MeshStandardMaterial({color:'#7e8386',roughness:.94});
  const pebbles=new THREE.InstancedMesh(new THREE.IcosahedronGeometry(1,1),pebbleMat,32);
  for(let i=0;i<32;i++){const a=random()*TAU,r=rand(3.7,4.4),x=Math.cos(a)*r,z=Math.sin(a)*r;dummy.position.set(x,surface(x,z)+.06,z);dummy.scale.set(rand(.12,.4),rand(.1,.3),rand(.14,.5));dummy.rotation.set(random(),random()*TAU,random());dummy.updateMatrix();pebbles.setMatrixAt(i,dummy.matrix);}pebbles.castShadow=true;world.add(pebbles);
}
function makeWater(){
  riverMat=new THREE.ShaderMaterial({side:THREE.DoubleSide,transparent:true,uniforms:{uTime:{value:0},uColor:{value:new THREE.Color('#64bbbf')},uWinter:{value:0}},vertexShader:'varying vec2 vUv; void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',fragmentShader:`varying vec2 vUv;uniform float uTime;uniform vec3 uColor;uniform float uWinter;void main(){float wave=sin(vUv.y*95.0-uTime*2.7+sin(vUv.x*14.0)*.7)*.5+.5;float glint=pow(wave,20.0)*.07;float edge=smoothstep(0.0,.2,vUv.x)*smoothstep(0.0,.2,1.0-vUv.x);vec3 c=uColor*(.65+wave*.12)+glint*(1.0-uWinter*.7);gl_FragColor=vec4(c,.75*edge);}`});
  const positions=[],uv=[],indices=[];
  for(let i=0;i<=100;i++){const z=-3.8+i/100*7.85,x=1.45+.62*Math.sin(z*.8);for(let j=0;j<2;j++){const xx=x+(j-.5)*.6;positions.push(xx,surface(xx,z)+.055,z);uv.push(j,i/100);}if(i<100){const k=i*2;indices.push(k,k+1,k+2,k+1,k+3,k+2);}}
  const geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));geo.setAttribute('uv',new THREE.Float32BufferAttribute(uv,2));geo.setIndex(indices);geo.computeVertexNormals();world.add(new THREE.Mesh(geo,riverMat));
  const fallGeo=new THREE.PlaneGeometry(.6,3.8,2,30),p=fallGeo.attributes.position;
  for(let i=0;i<p.count;i++){const y=p.getY(i),t=(1.9-y)/3.8;p.setXYZ(i,p.getX(i)+1.45+.62*Math.sin(4.05*.8),.38-t*3.8,4.05+.3*Math.sin(t*Math.PI*.7));}fallGeo.computeVertexNormals();
  const fallMat=riverMat.clone();fallMat.uniforms.uTime=riverMat.uniforms.uTime;fallMat.uniforms.uColor=riverMat.uniforms.uColor;fallMat.uniforms.uWinter=riverMat.uniforms.uWinter;fallMat.fragmentShader=`varying vec2 vUv;uniform float uTime;uniform vec3 uColor;uniform float uWinter;void main(){float edge=smoothstep(0.,.18,vUv.x)*smoothstep(0.,.18,1.-vUv.x);float strands=.55+.2*sin(vUv.x*75.+sin(vUv.y*12.+uTime*1.7));float sparkle=pow(max(0.,sin(vUv.y*48.+uTime*5.)),20.)*.08;float fade=smoothstep(0.,.75,vUv.y);gl_FragColor=vec4(uColor*(strands+sparkle),edge*fade*.7);}`;world.add(new THREE.Mesh(fallGeo,fallMat));
}
function makeRings(){
  ringMat=new THREE.MeshBasicMaterial({color:'#eebbd0',transparent:true,opacity:.24,blending:THREE.AdditiveBlending,depthWrite:false});
  for(let i=0;i<2;i++){
    const ring=new THREE.Mesh(new THREE.TorusGeometry(5.7+i*.9,.009,5,220),ringMat);ring.rotation.set(Math.PI/2+(i===0?.18:-.22),0,i===0?.14:-.18);ring.position.y=.95+i*.15;world.add(ring);
  }
  for(let j=0;j<3;j++){
    const positions=[],uv=[];
    for(let i=0;i<480;i++){const a=i/480*TAU,r=5.75+j*.35;positions.push(Math.cos(a)*r,1.0+Math.sin(a)*1.5,Math.sin(a)*r);uv.push(i/480,random());}
    const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));g.setAttribute('aData',new THREE.Float32BufferAttribute(uv,2));
    const m=new THREE.ShaderMaterial({transparent:true,depthWrite:false,blending:THREE.AdditiveBlending,uniforms:{uTime:{value:0},uOffset:{value:j*.33},uColor:{value:new THREE.Color('#f4c2d4')}},vertexShader:`attribute vec2 aData;varying float vAlpha;uniform float uTime;uniform float uOffset;void main(){float k=fract(aData.x-uTime*.06-uOffset);vAlpha=pow(k,10.0);vec4 p=modelViewMatrix*vec4(position,1.0);gl_Position=projectionMatrix*p;gl_PointSize=(1.0+vAlpha*3.0)*80.0/(-p.z);}`,fragmentShader:`uniform vec3 uColor;varying float vAlpha;void main(){float d=length(gl_PointCoord-.5)*2.0;gl_FragColor=vec4(uColor*2.5,pow(max(0.,1.-d),1.7)*vAlpha);}`});
    const points=new THREE.Points(g,m);points.rotation.z=j*.13;world.add(points);orbitTrails.push(points);
  }
  for(let i=0;i<9;i++){
    const rock=new THREE.Mesh(new THREE.IcosahedronGeometry(rand(.08,.26),0),new THREE.MeshStandardMaterial({color:'#83899e',roughness:.6,metalness:.3}));rock.userData={angle:random()*TAU,radius:rand(5.0,6.4),height:rand(-2.1,-.6),speed:rand(.06,.12)};world.add(rock);floatingRocks.push(rock);
  }
}
function makeWeather(){
  const count=620,positions=[],seeds=[],sizes=[],targets=[];
  for(let i=0;i<count;i++){
    positions.push(rand(-10,10),rand(-3,12),rand(-8,9));seeds.push(random(),random(),random());sizes.push(rand(.8,2.4));
    const side=Math.floor(random()*4),k=random(),vertices=[[0,1.35],[-1.35,0],[0,-1.35],[1.35,0]],a=vertices[side],b=vertices[(side+1)%4];
    targets.push(THREE.MathUtils.lerp(a[0],b[0],k)+rand(-.07,.07),4.0+THREE.MathUtils.lerp(a[1],b[1],k)+rand(-.07,.07),rand(-.1,.1));
  }
  const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));g.setAttribute('aSeed',new THREE.Float32BufferAttribute(seeds,3));g.setAttribute('aSize',new THREE.Float32BufferAttribute(sizes,1));g.setAttribute('aTarget',new THREE.Float32BufferAttribute(targets,3));
  particleMat=new THREE.ShaderMaterial({transparent:true,depthWrite:false,blending:THREE.NormalBlending,uniforms:{uTime:{value:0},uColor:{value:new THREE.Color('#eec0d2')},uWinter:{value:0},uSummer:{value:0},uAutumn:{value:0},uReveal:{value:0},uOffset:{value:new THREE.Vector3()},uPixel:{value:1},uLogoScale:{value:.9}},vertexShader:`attribute vec3 aSeed;attribute float aSize;attribute vec3 aTarget;uniform float uTime,uWinter,uSummer,uAutumn,uReveal,uPixel,uLogoScale;uniform vec3 uOffset;varying float vAlpha,vSeed;void main(){float a=aSeed.x*6.283185+uTime*(.10+uAutumn*.11);float r=3.8+aSeed.y*5.8;vec3 p=position;p.x=cos(a)*r+sin(uTime*.4+aSeed.z*10.)*.4;p.z=sin(a)*r;p.y=mod(position.y-uTime*(.22+uWinter*.6+uAutumn*.3)+15.,15.)-3.;p+=uOffset;vec3 target=vec3(aTarget.x*uLogoScale,4.0+(aTarget.y-4.0)*uLogoScale,aTarget.z);p=mix(p,target,uReveal);vec4 mv=modelViewMatrix*vec4(p,1.);gl_Position=projectionMatrix*mv;gl_PointSize=clamp(aSize*(68./-mv.z)*uPixel*(1.-uReveal*.7),1.,30.);vAlpha=(.18+aSeed.z*.46)*(1.-uSummer*.75)*(1.-uReveal*.72);vSeed=aSeed.x;}`,fragmentShader:`uniform vec3 uColor;uniform float uWinter,uSummer,uAutumn;varying float vAlpha,vSeed;void main(){vec2 p=(gl_PointCoord-.5)*2.;float a=atan(p.y,p.x),r=length(p);float flower=.66+.21*cos(a*5.0);float leaf=1.-abs(p.x)*.6-abs(p.y)*.38;float flake=.53+.32*pow(abs(cos(a*3.)),5.);float shape=mix(flower,leaf,uAutumn);shape=mix(shape,flake,uWinter);shape=mix(shape,.72,uSummer);float alpha=smoothstep(shape,shape-.13,r)*vAlpha;vec3 c=uColor*(.8+vSeed*.4);gl_FragColor=vec4(c,alpha);}`});
  particles=new THREE.Points(g,particleMat);particles.frustumCulled=false;scene.add(particles);
  const bgPos=[];for(let i=0;i<650;i++)bgPos.push(rand(-55,55),rand(-15,45),rand(-55,-12));
  const starsGeo=new THREE.BufferGeometry();starsGeo.setAttribute('position',new THREE.Float32BufferAttribute(bgPos,3));const stars=new THREE.Points(starsGeo,new THREE.PointsMaterial({color:'#a5b5d4',size:.027,transparent:true,opacity:.34,depthWrite:false}));scene.add(stars);
}
function makeSun(){
  sun=new THREE.Mesh(new THREE.SphereGeometry(.8,40,24),new THREE.MeshBasicMaterial({color:'#ffe9d1',transparent:true}));sun.position.set(-2.5,6.6,-5.8);world.add(sun);
  sunHalo=new THREE.Sprite(new THREE.SpriteMaterial({map:glowTexture(),color:'#ffd4b6',transparent:true,opacity:.45,blending:THREE.AdditiveBlending,depthWrite:false}));sunHalo.scale.set(5.5,5.5,1);sun.add(sunHalo);
  const halo=new THREE.Mesh(new THREE.TorusGeometry(1.08,.008,6,100),new THREE.MeshBasicMaterial({color:'#ffe0b9',transparent:true,opacity:.25}));sun.add(halo);
}
function makeLogo(){
  logo=new THREE.Group();logo.position.set(0,4,0);logo.scale.setScalar(.001);logo.visible=false;scene.add(logo);
  const shape=new THREE.Shape();shape.moveTo(0,1.12);shape.lineTo(-1.12,0);shape.lineTo(0,-1.12);shape.lineTo(1.12,0);shape.closePath();
  const hole=new THREE.Path();hole.moveTo(0,.77);hole.lineTo(.77,0);hole.lineTo(0,-.77);hole.lineTo(-.77,0);hole.closePath();shape.holes.push(hole);
  const g=new THREE.ExtrudeGeometry(shape,{depth:.15,bevelEnabled:true,bevelSegments:4,steps:1,bevelSize:.065,bevelThickness:.065});g.center();
  const mat=new THREE.MeshPhysicalMaterial({color:'#e3e6ff',metalness:.68,roughness:.21,clearcoat:1,clearcoatRoughness:.12,iridescence:.75,iridescenceIOR:1.4,iridescenceThicknessRange:[120,420],envMapIntensity:1.65});
  const frame=new THREE.Mesh(g,mat);logo.add(frame);
  const crystal=new THREE.Mesh(new THREE.OctahedronGeometry(.52),new THREE.MeshPhysicalMaterial({color:'#d7e4ff',metalness:.35,roughness:.12,clearcoat:1,iridescence:1,envMapIntensity:2}));crystal.rotation.z=Math.PI/4;logo.add(crystal);
  const diamond=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0,1.28,0),new THREE.Vector3(-1.28,0,0),new THREE.Vector3(0,-1.28,0),new THREE.Vector3(1.28,0,0),new THREE.Vector3(0,1.28,0)]);
  logo.add(new THREE.Line(diamond,new THREE.LineBasicMaterial({color:'#c4c9ff',transparent:true,opacity:.4})));
  const halo=new THREE.Sprite(new THREE.SpriteMaterial({map:glowTexture(),color:'#b0a0e8',transparent:true,opacity:.3,blending:THREE.AdditiveBlending,depthWrite:false}));halo.position.z=-.3;halo.scale.set(7,7,1);logo.add(halo);
  const colors=['#eaa9c9','#9dd2b4','#edb17b','#accdf5'];
  for(let i=0;i<4;i++){
    const geo=new THREE.TorusGeometry(1.7+i*.07,.008,5,90,Math.PI*.39),mesh=new THREE.Mesh(geo,new THREE.MeshBasicMaterial({color:colors[i],transparent:true,opacity:.5}));mesh.rotation.z=i*Math.PI/2;mesh.position.z=-.2;logo.add(mesh);
  }
}
function init(){
  renderer=new THREE.WebGLRenderer({antialias:true,alpha:false,powerPreference:'high-performance'});
  renderer.setPixelRatio(Math.min(devicePixelRatio,innerWidth<650?1.2:1.6));renderer.setSize(innerWidth,innerHeight);renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.02;renderer.info.autoReset=false;
  $('stage').appendChild(renderer.domElement);
  scene=new THREE.Scene();scene.background=new THREE.Color(config[0].background);scene.fog=new THREE.FogExp2(config[0].background,.013);
  camera=new THREE.PerspectiveCamera(39,innerWidth/innerHeight,.1,140);
  world=new THREE.Group();scene.add(world);
  const pmrem=new THREE.PMREMGenerator(renderer),room=new RoomEnvironment();const env=pmrem.fromScene(room,.04);environmentTarget=env;scene.environment=env.texture;scene.environmentIntensity=.24;room.dispose();pmrem.dispose();
  ambient=new THREE.HemisphereLight('#d7e3ff','#35243b',1.05);scene.add(ambient);
  keyLight=new THREE.DirectionalLight('#ffd6cc',2.3);keyLight.position.set(-3,12,8);keyLight.castShadow=true;keyLight.shadow.mapSize.set(1536,1536);keyLight.shadow.camera.left=-10;keyLight.shadow.camera.right=10;keyLight.shadow.camera.top=12;keyLight.shadow.camera.bottom=-6;keyLight.shadow.normalBias=.045;keyLight.shadow.bias=-.0001;scene.add(keyLight);
  rimLight=new THREE.DirectionalLight('#89b7ff',1.7);rimLight.position.set(7,5,-6);scene.add(rimLight);
  const fill=new THREE.PointLight('#e7acff',5,25,2);fill.position.set(0,-1,4);scene.add(fill);
  makeTerrain();makeTree();makeGroundDetails();makeWater();makeRings();makeWeather();makeSun();makeLogo();
  composer=new EffectComposer(renderer);composer.addPass(new RenderPass(scene,camera));composer.addPass(new UnrealBloomPass(new THREE.Vector2(innerWidth,innerHeight),.25,.55,1.18));composer.addPass(new OutputPass());
  renderer.domElement.addEventListener('webglcontextlost',event=>{if(disposed)return;event.preventDefault();fallback('3D 그래픽 연결이 중단되었습니다. 메인 플랫폼으로 이동할 수 있습니다.');});
  window.addEventListener('resize',resize);
  if(reduced&&!params.has('at')){timeline=DURATION;paused=true;}
  loaded=true;updateUI(true);animate();
}
function resize(){if(!renderer)return;camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setPixelRatio(Math.min(devicePixelRatio,innerWidth<650?1.2:1.6));renderer.setSize(innerWidth,innerHeight);composer.setSize(innerWidth,innerHeight);}
function updateUI(force=false){
  const index=Math.min(3,Math.floor(timeline/CHAPTER));
  const end=timeline>=28;
  if(lastChapter!==index||force){
    const c=config[index];clearTimeout(titleTimeout);
    const apply=()=>{$('season-title').innerHTML=c.title+'<span>.</span>';$('season-korean').textContent=c.line;$('season-description').innerHTML=c.description;$('chapter-number').textContent=String(index+1).padStart(2,'0');$('chapter-label').textContent=c.label;$('season-detail').textContent=c.detail;$('landscape-number').textContent=String(index+1).padStart(2,'0');$('landscape-caption').textContent=c.caption;$('story').classList.remove('changing');};
    if(force)apply();else{$('story').classList.add('changing');titleTimeout=setTimeout(apply,260);}
    lastChapter=index;$('live').textContent=end?'계절의 여정이 끝났습니다. 메인 플랫폼으로 이동하거나 다시 볼 수 있습니다.':c.name+' 장면. '+c.line;
    if(soundOn)tuneAudio(index);
  }
  if(end!==brandOn||force){
    brandOn=end;$('brand').hidden=!end;document.body.classList.toggle('ending',end);
    $('story').inert=end;$('controls').inert=end;
    $('skip').innerHTML=end?'REPLAY <span aria-hidden="true">↻</span>':'SKIP <span aria-hidden="true">↗</span>';
  }
  document.querySelectorAll('.season-nav').forEach((el,i)=>{el.classList.toggle('active',i===index&&!end);if(i===index&&!end)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');el.querySelector('i').style.transform=`scaleX(${clamp((timeline-i*CHAPTER)/CHAPTER)})`;});
  $('time-label').innerHTML='00:'+String(Math.floor(timeline)).padStart(2,'0')+' <i>/ 00:32</i>';
  $('pause-symbol').textContent=paused?'▷':'Ⅱ';$('pause').setAttribute('aria-label',paused?'애니메이션 재생':'애니메이션 일시정지');
  document.body.dataset.season=end?'brand':config[index].title.toLowerCase();
}
function updateMaterials(index,next,t){
  const a=config[index],b=config[next],winter=index===3?1:next===3?t:0,summer=index===1?1-t:next===1?t:0,autumn=index===2?1-t:next===2?t:0;
  const accent=blended(a.accent,b.accent,t);document.documentElement.style.setProperty('--accent','#'+accent.getHexString());
  scene.background.copy(blended(a.background,b.background,t));scene.fog.color.copy(scene.background);
  keyLight.color.copy(blended(a.light,b.light,t));rimLight.color.copy(blended(a.rim,b.rim,t));
  const colors=terrain.geometry.attributes.color,ground=blended(a.earth,b.earth,t).clone();
  for(let i=0;i<colors.count;i++){const v=terrainVariance[i];colors.setXYZ(i,ground.r*v,ground.g*v,ground.b*v);}colors.needsUpdate=true;
  grassMat.color.copy(blended(a.grass,b.grass,t));flowerMat.color.copy(blended(a.leaf2,b.leaf2,t));
  barkMat.color.set('#6b5148').lerp(new THREE.Color('#93a7ba'),winter*.65);
  const first=blended(a.leaf1,b.leaf1,t).clone(),second=blended(a.leaf2,b.leaf2,t).clone();
  for(const entry of seasonMaterialColors){for(let i=0;i<entry.variants.length;i++){mixColor.copy(first).lerp(second,entry.variants[i]);entry.mesh.setColorAt(i,mixColor);}entry.mesh.instanceColor.needsUpdate=true;}
  leafMat.color.set('#ffffff');leafMat.emissive.copy(first);
  for(const uniforms of [leafUniforms,snowUniforms,grassUniforms,flowerUniforms])uniforms.uWinter.value=winter;
  riverMat.uniforms.uColor.value.copy(blended(a.water,b.water,t));riverMat.uniforms.uWinter.value=winter;
  sun.material.color.copy(blended(a.sun,b.sun,t));sunHalo.material.color.copy(sun.material.color);
  particleMat.uniforms.uColor.value.copy(blended(a.accent,b.accent,t));particleMat.uniforms.uWinter.value=winter;particleMat.uniforms.uSummer.value=summer;particleMat.uniforms.uAutumn.value=autumn;
  ringMat.color.copy(blended(a.accent,b.accent,t));orbitTrails.forEach(p=>p.material.uniforms.uColor.value.copy(ringMat.color));
}
let lastMaterial=-999;
function animate(now=performance.now()){
  if(disposed)return;
  animationId=requestAnimationFrame(animate);
  const delta=safeFrameDelta(now,last);last=now;
  if(document.hidden)return;
  if(!paused){timeline=Math.min(DURATION,timeline+delta);visualTime+=delta;}
  const mobile=innerWidth<650;
  const index=Math.min(3,Math.floor(timeline/CHAPTER)),next=Math.min(3,index+1),transition=index<3?smooth((timeline%CHAPTER-5.25)/1.75):0;
  const reveal=smooth((timeline-28)/3.2);
  if(Math.abs(timeline-lastMaterial)>.055||firstFrame){updateMaterials(index,next,transition);lastMaterial=timeline;}
  smoothPointer.lerp(pointer,.035);
  world.position.set((mobile?0:2.6)*(1-reveal),(mobile?-1.7:-.1)*(1-reveal)+reveal*4,0);
  world.rotation.y=Math.sin(visualTime*.13)*.22+timeline*.044+reveal*1.7;world.rotation.z=Math.sin(visualTime*.21)*.012;
  world.scale.setScalar(Math.max(.001,(mobile?.76:1)*(1-reveal)));world.visible=reveal<.999;
  const sweep=Math.sin(Math.PI*transition);const distance=(mobile?28:22)-sweep*1.0,angle=.13+Math.sin(timeline*.115)*.20+sweep*.15+smoothPointer.x*.045;
  camera.position.set(Math.sin(angle)*distance*(1-reveal),THREE.MathUtils.lerp(mobile?8.5:7.5,4.3,reveal)+smoothPointer.y*.3,THREE.MathUtils.lerp(Math.cos(angle)*distance,15.6,reveal));
  camera.lookAt(0,THREE.MathUtils.lerp(mobile?1.0:1.6,1.9,reveal),0);
  leafUniforms.uTime.value=visualTime;snowUniforms.uTime.value=visualTime;grassUniforms.uTime.value=visualTime;flowerUniforms.uTime.value=visualTime;
  riverMat.uniforms.uTime.value=visualTime;
  particleMat.uniforms.uTime.value=visualTime;particleMat.uniforms.uReveal.value=reveal;particleMat.uniforms.uOffset.value.copy(world.position).multiplyScalar(1-reveal);particleMat.uniforms.uPixel.value=renderer.getPixelRatio();particleMat.uniforms.uLogoScale.value=mobile?.78:.9;sun.visible=!mobile;
  orbitTrails.forEach(p=>p.material.uniforms.uTime.value=visualTime+reveal*10);
  floatingRocks.forEach((rock,i)=>{const d=rock.userData,a=d.angle+visualTime*d.speed;rock.position.set(Math.cos(a)*d.radius,d.height+Math.sin(visualTime*.5+i)*.15,Math.sin(a)*d.radius);rock.rotation.x=visualTime*.12;rock.rotation.z=visualTime*.08;});
  logo.visible=reveal>.08;logo.scale.setScalar(smooth((reveal-.08)/.82)*(mobile?.78:.9));logo.rotation.y=Math.sin(visualTime*.5)*.20+smoothPointer.x*.2;logo.rotation.x=Math.sin(visualTime*.31)*.07+smoothPointer.y*.09;logo.position.y=4+Math.sin(visualTime*.6)*.035;
  keyLight.position.x=THREE.MathUtils.lerp(-3,1,reveal);
  updateUI();renderer.info.reset();composer.render();frame++;
  if(firstFrame){firstFrame=false;$('loader').classList.add('done');setTimeout(()=>$('loader').hidden=true,850);window.__introReady=true;notifyParent('mlo:cinematic:ready');}
  window.__introState={time:Number(timeline.toFixed(2)),season:document.body.dataset.season,paused,webgl:true,drawCalls:renderer.info.render.calls,triangles:renderer.info.render.triangles};
}
function reset(){timeline=0;visualTime=0;paused=false;lastMaterial=-999;updateUI(true);}
function finish(){timeline=DURATION;paused=false;lastMaterial=-999;updateUI(true);if(!loaded){document.body.classList.add('ending');$('brand').hidden=false;}}
$('skip').addEventListener('click',()=>brandOn?reset():finish());$('replay').addEventListener('click',()=>loaded?reset():location.reload());$('home').addEventListener('click',enterPlatform);$('enter').addEventListener('click',enterPlatform);$('pause').addEventListener('click',()=>{paused=!paused;updateUI();});
$('fallback-enter').addEventListener('click',()=>{finish();$('error').hidden=true;});
document.querySelectorAll('[data-season]').forEach(el=>el.addEventListener('click',()=>{timeline=Number(el.dataset.season)*CHAPTER+.01;paused=false;lastMaterial=-999;updateUI(true);}));
window.addEventListener('pointermove',e=>pointer.set((e.clientX/innerWidth-.5)*2,(e.clientY/innerHeight-.5)*2),{passive:true});
window.addEventListener('keydown',e=>{if(e.target instanceof HTMLElement&&['BUTTON','A','INPUT'].includes(e.target.tagName))return;if(e.code==='Space'){e.preventDefault();paused=!paused;updateUI();}else if(e.code==='Escape')finish();});
document.addEventListener('visibilitychange',()=>{last=performance.now();if(audioGain&&audioContext)audioGain.gain.setTargetAtTime(document.hidden?0:soundOn?.055:0,audioContext.currentTime,.3);});
function tuneAudio(index){
  if(!audioContext)return;const chords=[[130.81,196,261.63,329.63],[146.83,220,293.66,369.99],[130.81,174.61,220,261.63],[110,164.81,220,261.63]];
  audioVoices.forEach((voice,i)=>voice.frequency.setTargetAtTime(chords[index][i],audioContext.currentTime,2));
}
$('sound').addEventListener('click',async()=>{
  try{
    if(!audioContext){audioContext=new (window.AudioContext||window.webkitAudioContext)();audioGain=audioContext.createGain();audioGain.gain.value=0;const low=audioContext.createBiquadFilter();low.type='lowpass';low.frequency.value=650;low.connect(audioGain);audioGain.connect(audioContext.destination);for(let i=0;i<4;i++){const o=audioContext.createOscillator(),g=audioContext.createGain();o.type='sine';g.gain.value=.25;o.connect(g);g.connect(low);o.start();audioVoices.push(o);}tuneAudio(Math.min(3,Math.floor(timeline/7)));}
    await audioContext.resume();soundOn=!soundOn;audioGain.gain.setTargetAtTime(soundOn?.055:0,audioContext.currentTime,.8);$('sound').setAttribute('aria-pressed',String(soundOn));$('sound').setAttribute('aria-label',soundOn?'배경음 끄기':'배경음 켜기');$('sound-label').textContent=soundOn?'SOUND ON':'SOUND OFF';
  }catch(error){console.warn('Optional sound unavailable:',error.name);}
});
function fallback(message){document.body.classList.add('no-webgl');$('loader').hidden=true;$('error').hidden=false;console.warn(message);finish();window.__introState={time:DURATION,season:'brand',webgl:false};notifyParent('mlo:cinematic:ready');}
try{init();}catch(error){console.warn('Seasonal intro renderer unavailable:',error.name);fallback(error.message);}

// Closing the frame must release animation/audio/GPU resources before the app opens.
window.addEventListener('pagehide',()=>{
  disposed=true;cancelAnimationFrame(animationId);clearTimeout(titleTimeout);
  if(audioContext&&audioContext.state!=='closed')void audioContext.close().catch(()=>{});
  if(scene){
    const geometries=new Set(),materials=new Set(),textures=new Set();
    scene.traverse(object=>{
      if(object.geometry)geometries.add(object.geometry);
      if(object.material)for(const material of(Array.isArray(object.material)?object.material:[object.material])){
        materials.add(material);for(const value of Object.values(material))if(value?.isTexture)textures.add(value);
      }
    });
    textures.forEach(value=>value.dispose());geometries.forEach(value=>value.dispose());materials.forEach(value=>value.dispose());
  }
  environmentTarget?.dispose();composer?.dispose();renderer?.dispose();
},{once:true});
