export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const freshness = (v, now=Date.now()) => { const age=now-Date.parse(v.event_time); return age >= -60000 && age <= 120000; };
export function delayLabel(p) {
  if (!p || p.status !== 'ok' || !Number.isFinite(p.delay_s)) return 'Недоступен';
  const n=Math.round(p.delay_s/6)/10;
  return `${n>0?'+':n<0?'−':''}${Math.abs(n).toLocaleString('ru-RU')} мин`;
}
export function parseFrames(buffer) {
  const frames=buffer.split(/\r?\n\r?\n/), rest=frames.pop(), events=[];
  for (const frame of frames) {
    let id='',type='message',data=[];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith('id:')) id=line.slice(3).trim();
      if (line.startsWith('event:')) type=line.slice(6).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    }
    if(data.length) events.push({id,type,data:JSON.parse(data.join('\n'))});
  }
  return {events,rest};
}
export function project(lon,lat,z) {
  const s=256*2**z,phi=Math.max(-85.0511,Math.min(85.0511,lat))*Math.PI/180;
  return [(lon+180)/360*s,(1-Math.log(Math.tan(phi)+1/Math.cos(phi))/Math.PI)/2*s];
}
export function unproject(x,y,z) {
  const s=256*2**z;
  return [x/s*360-180,Math.atan(Math.sinh(Math.PI*(1-2*y/s)))*180/Math.PI];
}
