import {parseFrames} from './model.js';
export class ApiError extends Error { constructor(status, code) { super(code); this.status=status; } }
export async function request(path,token,signal,options={}) {
  const response=await fetch(`/api/v1/${path}`,{...options,signal,headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json',...options.headers}});
  if(!response.ok) { const body=await response.json().catch(()=>({})); throw new ApiError(response.status,body.error_code || `HTTP ${response.status}`); }
  return response.json();
}
export async function events(run,cursor,token,signal,onEvent,onConnected) {
  const response=await fetch(`/api/v1/events?run_id=${encodeURIComponent(run)}&after=${cursor}`,{signal,headers:{Authorization:`Bearer ${token}`}});
  if(!response.ok) throw new ApiError(response.status,`HTTP ${response.status}`);
  onConnected();
  const reader=response.body.getReader(),decoder=new TextDecoder(); let buffer='';
  try { while(true) {
    const {value,done}=await reader.read(); if(done) throw new Error('Поток закрыт');
    buffer+=decoder.decode(value,{stream:true});
    const parsed=parseFrames(buffer); buffer=parsed.rest;
    for(const event of parsed.events) onEvent(event);
  } } finally { await reader.cancel().catch(()=>{}); reader.releaseLock(); }
}
