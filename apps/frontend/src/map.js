import {project,unproject,freshness} from './model.js';
export class TransitMap {
  constructor(el,onSelect) {
    this.el=el;this.onSelect=onSelect;this.center=[37.62,55.755];this.zoom=11;this.vehicles=[];this.selected=null;this.tiles=new Map();
    el.innerHTML='<div class="tiles"></div><div class="markers"></div><div class="map-empty">Нет координат в выбранной выборке</div><div class="map-note">Подложка загружается…</div>';
    this.tileLayer=el.querySelector('.tiles');this.markerLayer=el.querySelector('.markers');
    window.addEventListener('online',()=>{for(const [key,tile] of this.tiles)if(tile.dataset.state==='error'){tile.remove();this.tiles.delete(key);}this.render();});
    this.observer=new ResizeObserver(()=>this.render());this.observer.observe(el);
    let drag=null;
    el.addEventListener('pointerdown',e=>{if(e.target.closest('button'))return;drag={x:e.clientX,y:e.clientY,center:project(...this.center,this.zoom)};el.setPointerCapture(e.pointerId);});
    el.addEventListener('pointermove',e=>{if(!drag)return;this.center=unproject(drag.center[0]-e.clientX+drag.x,drag.center[1]-e.clientY+drag.y,this.zoom);this.render();});
    el.addEventListener('pointerup',()=>{drag=null;});el.addEventListener('pointercancel',()=>{drag=null;});
    el.addEventListener('keydown',e=>{const delta={ArrowLeft:[-100,0],ArrowRight:[100,0],ArrowUp:[0,-100],ArrowDown:[0,100]}[e.key];if(delta){e.preventDefault();const c=project(...this.center,this.zoom);this.center=unproject(c[0]+delta[0],c[1]+delta[1],this.zoom);this.render();}});
  }
  tileStatus(){
    const values=[...this.tiles.values()];
    const loading=values.some(t=>t.dataset.state==='loading'),failed=values.some(t=>t.dataset.state==='error');
    this.el.querySelector('.map-note').textContent=loading?'Загрузка подложки…':failed?'Часть подложки недоступна · позиции по координатам':'© OpenStreetMap contributors';
  }
  update(vehicles,selected){this.vehicles=vehicles;this.selected=selected;this.render();}
  step(n){this.zoom=Math.max(3,Math.min(18,this.zoom+n));this.render();}
  focus(v){if(v && Number.isFinite(v.lon)&&Number.isFinite(v.lat)){this.center=[v.lon,v.lat];this.zoom=14;}this.render();}
  reset(){this.center=[37.62,55.755];this.zoom=11;this.render();}
  render(){
    const w=this.el.clientWidth,h=this.el.clientHeight;if(!w||!h)return;
    const [cx,cy]=project(...this.center,this.zoom),left=cx-w/2,top=cy-h/2,needed=new Set();
    for(let x=Math.floor(left/256);x<=Math.floor((left+w)/256);x++)for(let y=Math.floor(top/256);y<=Math.floor((top+h)/256);y++){
      if(y<0||y>=2**this.zoom)continue;
      const key=`${this.zoom}/${x}/${y}`;needed.add(key);let tile=this.tiles.get(key);
      if(!tile){tile=document.createElement('img');tile.alt='';tile.draggable=false;tile.src=`https://tile.openstreetmap.org/${this.zoom}/${((x%2**this.zoom)+2**this.zoom)%2**this.zoom}/${y}.png`;tile.dataset.state='loading';tile.onload=()=>{tile.dataset.state='loaded';this.tileStatus();};tile.onerror=()=>{tile.dataset.state='error';this.tileStatus();};this.tiles.set(key,tile);this.tileLayer.append(tile);}
      tile.style.transform=`translate(${x*256-left}px,${y*256-top}px)`;
    }
    for(const [key,tile]of this.tiles)if(!needed.has(key)){tile.remove();this.tiles.delete(key);}
    this.tileStatus();this.markerLayer.replaceChildren();let count=0;
    for(const v of this.vehicles){if(!Number.isFinite(v.lat)||!Number.isFinite(v.lon))continue;count++;const [x,y]=project(v.lon,v.lat,this.zoom);const marker=document.createElement('button');marker.className=`map-marker ${freshness(v)&&v.location_valid?'':'stale'} ${v.tr_id===this.selected?'selected':''}`;marker.style.left=`${x-left}px`;marker.style.top=`${y-top}px`;marker.textContent='↗';marker.title=`ТС ${v.tr_id}${v.location_valid?'':' · последняя пригодная позиция'}`;marker.setAttribute('aria-label',`Показать ТС ${v.tr_id}`);marker.onclick=()=>this.onSelect(v.tr_id);this.markerLayer.append(marker);}
    this.el.querySelector('.map-empty').hidden=count>0;
  }
}
