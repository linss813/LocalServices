'use strict';
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const colors=['#b6f171','#82b9ff','#c4a1f3','#f5ba76','#76d3cd','#647386'];
const preference={get:key=>{try{return window.localStorage.getItem(key)}catch{return null}},set:(key,value)=>{try{window.localStorage.setItem(key,value)}catch{}}};
const types={custom:'自定义服务',brew:'Homebrew',launchd:'macOS 启动器',systemd:'systemd',windows:'Windows 服务',process:'端口进程 · 只读',group:'进程资源组 · 只读',socket:'未归属端口 · 只读'};
let data=null,services=[],busy=false,refreshing=false,focused=null,currentView='overview',historyFollow=true,notified=null;
let selectedNode=preference.get('localServiceNode')||'local',nodeList=[],sortAsc=preference.get('localServiceSortAsc')==='true',refreshSeq=0,aliasTarget=null,servicePage=1,runtimeMessage='';
const display=s=>s.displayName||s.name;
function bytes(n){if(n==null)return '不可用';if(n>=1073741824)return (n/1073741824).toFixed(1)+' GiB';if(n>=1048576)return (n/1048576).toFixed(1)+' MiB';if(n>=1024)return (n/1024).toFixed(1)+' KiB';return Math.round(n)+' B'}
const rate=n=>n==null?'采样中 / 不可用':bytes(n)+'/s';
const percent=n=>n==null?'采样中':n.toFixed(1)+'%';
const duration=n=>n==null?'—':n<60?Math.floor(n)+' 秒':n<3600?Math.floor(n/60)+' 分':n<86400?Math.floor(n/3600)+' 时 '+Math.floor(n%3600/60)+' 分':Math.floor(n/86400)+' 天';
const stamp=t=>t?new Date(t*1000).toLocaleString():'—';
function metric(n,key){return key==='cpu'?percent(n):key==='memory'?bytes(n):rate(n)}
async function api(path,body){const global=['nodes','node-add','node-remove','node-access'].includes(path);const response=await fetch('/api/'+path+(global?'':'?node='+encodeURIComponent(selectedNode)),body?{method:'POST',headers:{'Content-Type':'application/json','X-Manager-Token':token},body:JSON.stringify(body)}:{});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result}
function setView(view){currentView=view;document.querySelectorAll('.view').forEach(s=>s.hidden=s.id!==view);document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('selected',b.dataset.view===view));if(data)renderActiveView()}
function focusGroup(id){focused=id;$('serviceScope').value='resources';$('search').value='';$('kind').value='';$('state').value='';setView('services');renderServices()}
function donut(target,key){const all=data.resources.filter(g=>(g[key]||0)>0).sort((a,b)=>b[key]-a[key]);const sum=all.reduce((n,g)=>n+g[key],0);let slices=all.slice(0,5).map(g=>({...g}));const other=all.slice(5).reduce((n,g)=>n+g[key],0);if(other)slices.push({name:'其他可见进程',id:null,[key]:other});const C=2*Math.PI*72;let offset=0;
const rings=slices.map((g,i)=>{const value=g[key]/sum*C;const length=Math.max(0,value-2);const node=`<circle class="segment" cx="100" cy="100" r="72" fill="none" stroke="${colors[i]}" stroke-width="22" stroke-dasharray="${length} ${C-length}" stroke-dashoffset="${-offset}" transform="rotate(-90 100 100)" ${g.id?`data-group="${esc(g.id)}" tabindex="0" role="button" aria-label="定位 ${esc(display(g))}"`:''}><title>${esc(display(g))}：${esc(metric(g[key],key))}，占可见进程 ${(g[key]/sum*100).toFixed(1)}%</title></circle>`;offset+=value;return node}).join('');
const waiting=key==='cpu'&&data.resources.every(g=>g.cpu==null);
$(target).innerHTML=`<svg class="donut" viewBox="0 0 200 200" role="img" aria-label="${key==='cpu'?'CPU':'内存'}占用分布，可见进程合计 ${esc(metric(sum,key))}"><circle cx="100" cy="100" r="72" fill="none" stroke="#2c3744" stroke-width="22"/>${rings}<text x="100" y="96" class="big">${waiting?'采样中':esc(metric(sum,key))}</text><text x="100" y="117">${key==='cpu'?'可见进程 CPU 合计':'可见进程 RSS 合计'}</text></svg><div class="legend">${slices.length?slices.map((g,i)=>`<button ${g.id?`data-group="${esc(g.id)}"`: 'disabled'} title="${esc(display(g))}"><span class="swatch" style="background:${colors[i]}"></span><span class="name">${esc(display(g))}</span><span class="amount">${(g[key]/sum*100).toFixed(1)}% · ${esc(metric(g[key],key))}</span></button>`).join(''):`<p>${waiting?'等待第二次后台采样':'当前没有可见占用'}</p>`}</div>`}
function ranking(){if(!data)return;const key=$('rankMetric').value;const items=[...data.resources].filter(g=>g[key]!=null).sort((a,b)=>b[key]-a[key]).slice(0,8);const max=items[0]?.[key]||1;$('rankTotal').textContent='TOP '+items.length;
$('ranking').innerHTML=items.length?items.map((g,i)=>`<div class="rank-row ${focused===g.id?'focused':''}"><span class="rank-number">${String(i+1).padStart(2,'0')}</span><div><button data-group="${esc(g.id)}" title="${esc(display(g))} · ${g.pids.length} 个进程">${esc(display(g))} <small>· ${g.pids.length} 进程</small></button><div class="bar"><i style="width:${g[key]/max*100}%;background:${colors[i%5]}"></i></div></div><span class="rank-value">${esc(metric(g[key],key))}</span></div>`).join(''):'<p>该平台没有可读取的此类指标，或正在等待第二次采样。</p>'}
function trend(){if(!data)return;const history=data.history||[];const selected=$('trendMetric').value;const keys={cpu:['cpu'],memory:['memory'],network:['recv','sent'],disk:['read','write']}[selected];const isPercent=selected==='cpu'||selected==='memory';const max=isPercent?100:Math.max(1024,...history.flatMap(h=>keys.map(k=>h[k]||0)));const W=Math.max(300,Math.min(610,$('trend').clientWidth||610)),H=184,left=53,top=16,width=W-left-13,height=128;const x=i=>left+(history.length>1?i/(history.length-1):0)*width;const y=v=>top+height*(1-v/max);const cursor=$('historyCursor');cursor.max=Math.max(0,history.length-1);if(historyFollow)cursor.value=cursor.max;const index=Math.min(Number(cursor.value),history.length-1);const point=history[index];
const labels=[max,max/2,0].map(v=>`<line x1="${left}" x2="${left+width}" y1="${y(v)}" y2="${y(v)}" stroke="#2c3744" stroke-dasharray="3 5"/><text x="${left-7}" y="${y(v)+4}" text-anchor="end">${isPercent?Math.round(v)+'%':esc(bytes(v))}</text>`).join('');
let lines='';keys.forEach((key,ki)=>{let chunks=[],points=[];history.forEach((h,i)=>{if(h[key]==null){if(points.length)chunks.push(points);points=[]}else points.push(`${x(i)},${y(h[key])}`)});if(points.length)chunks.push(points);lines+=chunks.map(points=>`<polyline points="${points.join(' ')}" stroke="${colors[ki]}" fill="none" stroke-width="2.2" stroke-linejoin="round"/>`).join('');if(point?.[key]!=null)lines+=`<circle cx="${x(index)}" cy="${y(point[key])}" r="4" fill="${colors[ki]}"><title>${esc(stamp(point.time))} ${esc(isPercent?percent(point[key]):rate(point[key]))}</title></circle>`});
$('trend').innerHTML=`<svg viewBox="0 0 ${W} ${H}" class="trend-svg" role="img" aria-label="${esc(selected)} 最近采样趋势">${labels}${lines}<text x="${left}" y="174">${history.length?esc(new Date(history[0].time*1000).toLocaleTimeString()):'—'}</text><text x="${left+width}" y="174" text-anchor="end">${history.length?esc(new Date(history.at(-1).time*1000).toLocaleTimeString()):'—'}</text></svg>`;
$('trendLabel').textContent=history.length+' / 120 个采样点 · '+(keys.length===2?'绿：'+(selected==='network'?'接收':'读')+' / 蓝：'+(selected==='network'?'发送':'写'):'后台持续采集');$('trendValue').textContent=point?stamp(point.time)+' · '+keys.map(k=>isPercent?percent(point[k]):rate(point[k])).join(' / '):'等待采样';}
function sortValue(service,key){if(key==='name')return display(service).toLocaleLowerCase();if(key==='ports')return service.ports.length?Math.min(...service.ports):null;return service[key]??null}
function compareServices(a,b,key,asc){const av=sortValue(a,key),bv=sortValue(b,key);if(av==null&&bv==null)return a.name.localeCompare(b.name);if(av==null)return 1;if(bv==null)return -1;const result=typeof av==='string'?av.localeCompare(bv):av-bv;return (asc?result:-result)||a.name.localeCompare(b.name)||String(a.id).localeCompare(String(b.id))}
function sortLabel(){const key=$('sort').value;$('sortDirection').textContent=(sortAsc?'↑ 升序':'↓ 降序');$('sortDirection').setAttribute('aria-label','当前'+(sortAsc?'升序':'降序')+'，点击切换');document.querySelectorAll('th [data-sort]').forEach(button=>{const active=button.dataset.sort===key;button.parentElement.setAttribute('aria-sort',active?(sortAsc?'ascending':'descending'):'none');button.textContent=button.textContent.replace(/ [↕↑↓]$/,'')+' '+(active?(sortAsc?'↑':'↓'):'↕')});preference.set('localServiceSort',key);preference.set('localServiceSortAsc',String(sortAsc))}
function renderServices(){if(!data)return;const q=$('search').value.toLowerCase();const sort=$('sort').value;const list=visibleServices();const items=list.filter(s=>(!focused||s.resourceId===focused)&&(!$('kind').value||(s.nativeKind||s.kind)===$('kind').value)&&(!$('state').value||Boolean(s.running)===($('state').value==='running'))&&[s.name,display(s),s.description,s.pid,...s.ports].join(' ').toLowerCase().includes(q)).sort((a,b)=>compareServices(a,b,sort,sortAsc));sortLabel();$('total').textContent=`${items.length} / ${list.length}`;$('empty').hidden=items.length>0;$('focusFilter').hidden=!focused;if(focused){const group=data.resources.find(g=>g.id===focused);$('focusFilter').innerHTML='资源组：'+esc(group?display(group):focused)+' <button id="clearFocus">清除定位筛选</button>'}
const pageCount=Math.max(1,Math.ceil(items.length/50));servicePage=Math.min(servicePage,pageCount);
const shown=items.slice((servicePage-1)*50,servicePage*50);
$('servicePageInfo').textContent='第 '+servicePage+' / '+pageCount+' 页 · 共 '+items.length+' 项（每页 50 项）';
$('servicePrevious').disabled=servicePage<=1;$('serviceNext').disabled=servicePage>=pageCount;
const watched=new Set(data.rules.watched);const control=Boolean(data.capabilities.control);$('rows').innerHTML=shown.map(s=>{const id=esc(s.id),disabled=busy?'disabled':'';return `<tr><td><div class="service-name"><div><div class="service" title="${esc(s.description)}">${esc(display(s))}</div><div class="tag service-sub identifier">${esc(s.name)}</div></div><button data-info="${id}">说明</button></div><div class="tag">${esc(types[s.kind]||s.kind)}${s.scope?' · '+esc(s.scope):''}${s.shared?' · 共享宿主资源':''}</div></td><td><span class="${s.running?'status':'stopped'}">● ${s.running?'运行中':'已停止'}</span><div class="tag">${s.pid?'PID '+s.pid:s.exitCode&&s.exitCode!=='0'&&s.exitCode!=='-'?'退出码 '+esc(s.exitCode):esc(s.state||'—')}</div></td><td class="port">${s.ports.join(' · ')||'—'}</td><td>${s.kind==='socket'?'不可用':s.running?percent(s.cpu):'—'}</td><td>${s.running?bytes(s.memory):'—'}</td><td>${s.running?esc(rate(s.readRate))+'<br>'+esc(rate(s.writeRate)):'—'}</td><td>${duration(s.uptime)}</td><td>${['process','group','socket'].includes(s.kind)?'—':`<div class="toggles"><label class="check"><input type="checkbox" data-id="${id}" data-op="autostart" ${s.autostart?'checked':''} ${disabled} ${!control||s.canAutostart===false?'disabled':''}>自启${!control?'（只读）':''}</label><label class="check"><input type="checkbox" data-id="${id}" data-op="watch" ${watched.has(s.id)?'checked':''} ${disabled}>离线告警</label></div>`}</td><td class="web-cell">${renderWebLinks(s)}</td><td><div class="actions">${['process','group','socket'].includes(s.kind)?'<small>系统发现</small>':!control?'<small>仅监控节点</small>':`<button data-id="${id}" data-op="${s.running?'stop':'start'}" ${disabled}>${s.running?'停止':'启动'}</button><button data-id="${id}" data-op="restart" ${disabled}>重启</button>${s.kind==='custom'?`<button data-id="${id}" data-op="logs">日志</button><button data-id="${id}" data-op="delete" ${disabled}>移除</button>`:''}`}</div></td></tr>`}).join('')}
function renderNetwork(){const n=data.network;$('interfaces').innerHTML=n.interfaces.map(i=>`<tr><td>${esc(i.name)}<div class="tag ${i.up?'safe-text':''}">${i.up?'已连接':'未连接'}</div></td><td class="port">${i.addresses.map(esc).join('<br>')||'—'}</td><td>${rate(i.recvRate)}</td><td>${rate(i.sentRate)}</td><td>${bytes(i.recv)} / ${bytes(i.sent)}</td><td>${i.errors} / ${i.drops}</td><td>${i.speed>0?i.speed+' Mbps':'未知'}</td></tr>`).join('');$('listeners').innerHTML=data.security.listeners.map(l=>`<tr><td>${esc(l.name)}<div class="tag">PID ${l.pid||'未知'}</div></td><td>${esc(l.protocol)}</td><td class="port">${esc(l.address)}</td><td>${l.port}</td><td><span class="pill ${l.exposed?'warning':'safe'}">${l.exposed?'非回环绑定':'仅回环'}</span></td></tr>`).join('');$('connectionSummary').textContent=n.complete?Object.entries(n.connections).map(([k,v])=>k+': '+v).join(' · '):'连接总数不可用 · 仅展示可见监听端口'}
function storageBytes(n){
 if(n==null)return '不可用';
 for(const [scale,label] of [[1e12,'TB'],[1e9,'GB'],[1e6,'MB'],[1e3,'kB']])if(n>=scale)return (n/scale).toFixed(scale===1e12?2:1)+' '+label;
 return n+' B';
}
function storageView(){
 const raw=data?.storage;if(!raw)return null;
 const eligible=raw.volumes.filter(v=>v.alertable);
 const options=[{id:'system',label:'系统盘'},{id:'all',label:'全部存储（含外接）'},{id:'external',label:'仅外接存储'},
 ...eligible.map(v=>({id:'volume:'+v.id,label:v.mount+' · '+({internal:'内置',external:'外接',unknown:'类型未知',network:'网络'}[v.location]||'存储卷')}))];
 const saved=preference.get('storageScope:'+selectedNode)||'system';
 const id=options.some(o=>o.id===saved)?saved:'system';
 const volumes=eligible.filter(v=>id==='all'||id==='system'&&(v.isSystem??v.mount==='/')||id==='external'&&v.location==='external'||id==='volume:'+v.id);
 const total=volumes.reduce((s,v)=>s+v.total,0),used=volumes.reduce((s,v)=>s+v.used,0),free=volumes.reduce((s,v)=>s+v.free,0);
 const reserved=volumes.reduce((s,v)=>s+(v.reserved||0),0);
 const ioNames=new Set(volumes.flatMap(v=>v.ioDevices||[]));
 const devices=data.diskIO.devices.filter(d=>ioNames.has(d.name));
 const sumRate=key=>{const values=devices.map(d=>d[key]).filter(v=>v!=null);return values.length?values.reduce((s,v)=>s+v,0):null};
 return {raw,id,label:options.find(o=>o.id===id).label,options,volumes,total,used,free,reserved,
 percent:total?used/total*100:null,readRate:sumRate('readRate'),writeRate:sumRate('writeRate'),devices};
}
function renderStorageHeader(){
 const s=storageView();$('diskScopeLabel').textContent=(s?.label||'系统盘')+'占用';
 $('diskRate').textContent=s&&s.total?percent(s.percent):'未识别容量';
 $('diskDetail').textContent=s&&s.total?'共 '+storageBytes(s.total)+' · 可用 '+storageBytes(s.free):'在存储页选择统计范围；不将其他磁盘冒充系统盘';
}
function renderStorage(){
 const s=storageView();
 if(!s){$('storageStats').innerHTML='<p>等待新版代理的存储汇总数据。</p>';['storageCapacity','volumes','mountTable','diskDevices'].forEach(id=>$(id).innerHTML='');return}
 const optionMarkup=s.options.map(o=>`<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('');
 if($('storageScope').innerHTML!==optionMarkup)$('storageScope').innerHTML=optionMarkup;
 $('storageScope').value=s.id;
 const busy=s.devices.filter(d=>d.busy!=null).sort((a,b)=>b.busy-a.busy)[0];
 $('storageStats').innerHTML=[
 ['范围内总容量',storageBytes(s.total),s.label+' · '+s.volumes.length+' 个去重存储池'],
 ['已占用',storageBytes(s.used),s.percent==null?'当前范围无可读取容量':percent(s.percent)+' · 包含系统保留'],
 ['可用空间',storageBytes(s.free),'不含可清理空间，排除只读系统镜像'],
 ['对应设备繁忙度',busy?percent(busy.busy):'不可用',busy?busy.name:'平台未提供或无法归属，未用 0% 代替']
 ].map(([title,value,note])=>`<article class="card"><small>${title}</small><div class="value">${esc(value)}</div><p>${esc(note)}</p></article>`).join('');
 const ratio=n=>s.total?n/s.total*100:0;
 const unit=s.total>=1e9?1e9:s.total>=1e6?1e6:1;const unitName=unit===1e9?'GB':unit===1e6?'MB':'B';
 const precise=n=>(n/unit).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+' '+unitName;
 $('storageCapacity').innerHTML=`
 <div class="panel-head"><h2>${esc(s.label)} · 容量使用率</h2><b>${s.percent==null?'不可用':percent(s.percent)}</b></div>
 <div class="capacity-parts" aria-label="容量组成">
 <i class="occupied" style="width:${ratio(s.used-s.reserved)}%"></i>
 <i class="reserved" style="width:${ratio(s.reserved)}%"></i>
 <i class="available" style="width:${ratio(s.free)}%"></i></div>
 <div class="capacity-legend"><span>已占用 ${storageBytes(s.used-s.reserved)}</span>
 ${s.reserved?'<span>系统保留 '+storageBytes(s.reserved)+'</span>':''}<span>可用 ${storageBytes(s.free)}</span></div>
 <p>计算：${precise(s.used)} ÷ ${precise(s.total)} × 100 ≈ ${s.percent==null?'不可用':percent(s.percent)}（按原始字节加权计算，容量显示已四舍五入，不是各盘百分比的平均）。
 ${s.id==='system'?'当前仅为系统存储池，未把外接盘计入。':s.id==='all'?'当前包含外接盘，大容量磁盘会占主要权重。':''}</p>
 <p>对应设备读取 ${rate(s.readRate)} · 写入 ${rate(s.writeRate)}；共享设备的 I/O 包括其其他卷。<br>
 快照共有 ${s.raw.mountCount} 个挂载项；辅助系统卷已折叠。范围切换只影响显示，告警仍分别检查每个可写存储池。</p>`;
 const expanded=new Set([...$('volumes').querySelectorAll('details[open][data-volume-detail]')].map(e=>e.dataset.volumeDetail));
 $('volumes').innerHTML=s.volumes.map(d=>`
 <article class="volume"><div class="mount">${esc(d.mount)}</div>
 <small>${esc(d.device)} · ${esc(d.fstype)} · ${{internal:'内置存储',external:'外接存储',network:'网络存储',unknown:'连接类型未知'}[d.location]||'连接类型未知'}</small>
 <div class="value">${percent(d.percent)} <small>已占用</small></div>
 <div class="bar ${d.percent>=data.rules.disk?'critical':''}"><i style="width:${d.percent}%"></i></div>
 <p>已占用 ${storageBytes(d.used)} / 共 ${storageBytes(d.total)}<br>可用 ${storageBytes(d.free)}<br>
 ${esc(d.capacitySource||'文件系统容量')}${d.capacitySampledAt?' · '+esc(new Date(d.capacitySampledAt*1000).toLocaleTimeString()):''}</p>
 <details data-volume-detail="${esc(d.id)}"><summary>${d.mounts.length} 个合并挂载</summary><p class="identifier">${d.mounts.map(esc).join('<br>')}</p></details></article>`
 ).join('')||'<p>当前范围未发现可读取的存储池，请切换范围。</p>';
 $('volumes').querySelectorAll('details[data-volume-detail]').forEach(e=>e.open=expanded.has(e.dataset.volumeDetail));
 $('mountCount').textContent='('+s.raw.mountCount+' 项)';
 $('mountTable').innerHTML=`<table><thead><tr><th>路径</th><th>设备 / 格式</th><th>用途</th></tr></thead><tbody>${
 s.raw.volumes.flatMap(d=>d.mounts.map(m=>`<tr><td class="port">${esc(m)}</td><td>${esc(d.device)}<div class="tag">${esc(d.fstype)}</div></td>
 <td>${d.alertable?'容量统计（同池已合并）':'辅助 / 只读系统卷 · 不参与告警'}</td></tr>`)).join('')}</tbody></table>`;
 $('diskDevices').innerHTML=data.diskIO.devices.map(d=>`
 <tr><td>${esc(d.name)}${s.devices.some(x=>x.name===d.name)?' · 当前范围':''}</td><td>${rate(d.readRate)}</td><td>${rate(d.writeRate)}</td>
 <td>${d.reads.toLocaleString()} / ${d.writes.toLocaleString()}</td><td>${d.busy==null?'不可用':percent(d.busy)}</td></tr>`
 ).join('')||'<tr><td colspan="5">磁盘 I/O 计数不可用</td></tr>';
}

function renderSecurity(){const s=data.security,f=s.firewall;const fw={enabled:'已启用',disabled:'未启用',unknown:'无法确认'}[f.state]||'未知';const exposed=s.exposed;const cards=[['防火墙',fw,f.scope+' · '+f.detail,f.state==='enabled'?'safe-text':'warning-text'],['非回环端口绑定',String(exposed.length),'绑定项数，包含不同地址与协议；不代表互联网可达。',exposed.length?'warning-text':'safe-text'],['监控代理权限',s.elevated?'管理员 / root':'普通用户','不自动提权；系统服务操作权限由操作系统判定。',s.elevated?'warning-text':'safe-text'],['可见 root 进程',String(s.rootProcesses),s.visibility+' · 仅统计可读取进程，非安全评分。','']];$('securityCards').innerHTML=cards.map(([title,value,detail,cls])=>`<article class="card"><small>${title}</small><div class="value ${cls}">${esc(value)}</div><p>${esc(detail)}</p></article>`).join('');const ports=exposed.filter(e=>[21,22,23,445,1433,2375,3306,3389,5432,5900,6379,9200,27017].includes(e.port));$('sensitivePorts').innerHTML=ports.length?ports.map(p=>`<div class="audit-row"><b class="warning-text">${p.port} / ${esc(p.protocol)}</b> · ${esc(p.name)} · <span class="port">${esc(p.address)}</span></div>`).join(''):'<p>当前可见范围内未发现常见敏感端口的非回环绑定。</p>';$('sessions').innerHTML=s.users.length?s.users.map(u=>`<div class="audit-row"><b>${esc(u.name)}</b> · ${esc(u.host||'本地')}<div class="tag">${esc(stamp(u.started))}</div></div>`).join(''):'<p>当前没有可读取的登录会话。</p>';$('audit').innerHTML=data.audit.length?data.audit.map(a=>`<div class="audit-row"><b>${esc(a.action)}</b> · ${esc(a.outcome)}<div class="tag">${esc(a.id)} · ${esc(stamp(a.time))}</div></div>`).join(''):'<p>尚无面板操作记录。</p>'}
function renderAlerts(){const active=data.alerts.filter(e=>!e.recovered);$('alertCount').textContent=active.length;$('alertList').innerHTML=data.alerts.length?data.alerts.slice(0,100).map(e=>`<article class="event ${e.recovered?'recovered':e.level}"><div class="panel-head"><h2>${e.suppressed?'✓ 已排除':e.recovered?'✓ 已恢复':e.level==='critical'?'● 严重':'● 注意'} · ${esc((e.target?e.target+' · ':'')+e.title)}</h2>${e.acknowledged?'<small>已确认</small>':`<button data-ack="${esc(e.id)}">确认知悉</button>`}</div><p>${esc(e.detail)}</p><div class="meta">触发 ${esc(stamp(e.time))}${e.recovered?' · 恢复 '+esc(stamp(e.recovered)):''}</div></article>`).join(''):'<div class="panel"><p>暂无告警。监控后台运行时会持续判断阈值。</p></div>';const r=data.rules;$('ruleSummary').textContent=`当前阈值：CPU ${r.cpu}% / 内存 ${r.memory}% / 磁盘 ${r.disk}% / 网络 ${r.networkMB} MB/s / 磁盘 I/O ${r.diskMB} MB/s / 服务内存 ${r.serviceMemoryMB} MB · 持续 ${r.sustain} 秒 · 磁盘持续 ${r.diskSustain??120} 秒 · 恢复稳定 ${r.recoverSustain??60} 秒 / 缓冲 ${r.hysteresis??5} 点 · 普通邮件间隔 ${r.emailInterval??300} 秒 · 持续告警仅通知一次 · ${r.watched.length} 个服务启用离线告警`;
const mail=data.email;$('emailStatus').innerHTML=`<div class="email-info"><div><h2>邮件告警 <span class="pill ${mail.enabled?'safe':'warning'}">${mail.enabled?'已配置':'未配置'}</span></h2><p>${esc(mail.sender||'—')} → ${esc(mail.recipient||'—')}<br>${esc(mail.host||'—')}:${mail.port||'—'} · ${mail.encryption==='none'?'非加密 SMTP':esc(mail.encryption.toUpperCase())} · 队列 ${mail.pending} 条</p><p class="${mail.last?.ok?'safe-text':'warning-text'}">${mail.sending?'正在发送…':mail.last?esc(stamp(mail.last.time))+' · '+esc(mail.last.detail):'尚无投递记录'}</p>${mail.encryption==='none'&&mail.enabled?'<p class="warning-text">当前按配置使用非加密连接；SMTP 认证和内容不受传输加密保护。服务商支持时建议改用 SSL 465。</p>':''}</div><button id="testEmail" ${!mail.enabled||mail.sending?'disabled':''}>发送测试邮件</button></div>`;
}
function notifyAlerts(){
 const current=new Set(data.alerts.map(e=>e.id));
 if(notified!==null)for(const event of data.alerts){
  if(!notified.has(event.id)&&!event.recovered&&!event.suppressed&&preference.get('localServiceNotifications')==='true'&&'Notification' in window&&Notification.permission==='granted'){
   new Notification(event.title,{body:event.detail,tag:(data.node?.id||selectedNode)+':'+event.key});
  }
 }
 notified=current;
}
function render(){if(!data)return;const mon=data.monitor||{};$('nodeStatus').innerHTML=`<b>${esc(data.node?.name||'监控节点')}</b><span>${data.capabilities.control?'服务控制已开放':'只读监控代理'}</span><span>采集 ${mon.sampleInterval||10} 秒 / 端口 ${mon.portsInterval||30} 秒</span><span>代理自身 CPU ${percent(mon.cpu)} · 内存 ${bytes(mon.memory)} · 采集 ${mon.collectMs??'—'} ms</span>`;$('openAdd').disabled=!data.capabilities.control;$('openAdd').title=data.capabilities.control?'注册自定义服务':'当前节点未开放服务控制';const h=data.host;$('platform').textContent={Darwin:'macOS',Linux:'Linux',Windows:'Windows'}[data.platform]||data.platform;$('cpu').textContent=percent(h.cpu);$('cpuBar').style.width=h.cpu+'%';$('cpuDetail').textContent=h.cores+' 核心 · '+h.processCount+' 个可见进程'+(h.load?' · 负载 '+h.load[0].toFixed(2):'');$('memory').textContent=bytes(h.memory);$('memoryDetail').textContent='共 '+bytes(h.total)+' · '+h.memoryPercent+'% · Swap '+bytes(h.swap);$('memoryBar').style.width=h.memoryPercent+'%';$('netRate').textContent='↓ '+rate(data.network.recvRate)+' / ↑ '+rate(data.network.sentRate);renderStorageHeader();$('updated').textContent='采样于 '+new Date(data.sampledAt*1000).toLocaleTimeString();$('live').textContent=data.age>Math.max(30,(data.monitor?.sampleInterval||10)*2)?'● 数据延迟 '+data.age+' 秒':'● 实时采集';$('live').className=data.age>Math.max(30,(data.monitor?.sampleInterval||10)*2)?'warning-text':'safe-text';$('summary').innerHTML=`<div><small>运行中服务 / 端口进程</small><b>${services.filter(s=>s.running).length} / ${services.length}</b></div><div><small>主机运行时长</small><b>${duration(Date.now()/1000-h.boot)}</b></div><div><small>当前告警</small><b>${data.alerts.filter(e=>!e.recovered).length} 条</b></div>`;$('alertCount').textContent=data.alerts.filter(e=>!e.recovered).length;renderActiveView();notifyAlerts();$('noticeCount').textContent=data.warnings.length?'· '+data.warnings.length+' 条':'';$('noticeBody').innerHTML=[...data.warnings,'进程网络流量归因不可用，网卡合计可能重复计入虚拟接口流量。','CPU 可见进程合计按多核累计，可超过 100%；环图比例为可见进程之间的相对份额。','系统服务管理需要相应权限；指标或防火墙无法读取时显示不可用，不视为安全。'].map(x=>'<p>'+esc(x)+'</p>').join('');if(data.error){runtimeMessage='采集失败，展示上次结果：'+data.error;$('message').textContent=runtimeMessage;$('live').textContent='● 采集异常（上次数据）';$('live').className='warning-text'}}
async function refresh(force=false){if(refreshing&&!force)return;if(!selectedNode){$('message').textContent='请添加或选择监控节点';return}const seq=++refreshSeq;refreshing=true;try{const result=await api('status');if(seq!==refreshSeq)return;if(result.loading){$('message').textContent=result.error?'后台采集失败：'+result.error:'代理正在采集首份数据…';return}const wasLoading=!data;data=result;services=data.services;document.body.classList.remove('offline-overlay');$('nodeStatus').classList.remove('error');if(wasLoading||$('message').textContent===runtimeMessage)$('message').textContent='';runtimeMessage='';render()}catch(e){if(seq!==refreshSeq)return;data=null;services=[];document.body.classList.add('offline-overlay');runtimeMessage='连接失败：'+e.message;$('message').textContent=runtimeMessage;$('nodeStatus').textContent='当前节点不可用：不展示其他节点或过期数据。代理如果仍在运行，会继续本机告警。';$('nodeStatus').classList.add('error');$('openAdd').disabled=true;$('live').textContent='● 连接中断';$('live').className='danger-text'}finally{if(seq===refreshSeq)refreshing=false}}
async function operate(id,action,enabled){if(busy||!data)return;if(action==='delete'&&!confirm('停止并移除此自定义服务？日志文件将保留。'))return;if(['stop','restart'].includes(action)&&!id.startsWith('custom:')&&!confirm('即将'+(action==='stop'?'停止':'重启')+'系统托管服务，依赖它的应用可能中断。是否继续？'))return;busy=true;renderServices();try{if(action==='logs'){const result=await api('logs',{id});$('logText').textContent=result.text;$('logs').showModal()}else{await api(action==='watch'?'watch':'action',{id,action,enabled});$('message').textContent=action==='watch'?'离线告警设置已保存':'操作已提交，下一次后台采样更新状态'}}catch(e){$('message').textContent=e.message}finally{busy=false;await refresh()}}
document.addEventListener('click',async event=>{const el=event.target.closest('[data-view],[data-close],[data-group],button[data-op],[data-ack]');if(el){if(el.dataset.view)setView(el.dataset.view);if(el.dataset.close)$(el.dataset.close).close();if(el.dataset.group)focusGroup(el.dataset.group);if(el.dataset.op)await operate(el.dataset.id,el.dataset.op);if(el.dataset.ack){try{await api('ack',{id:el.dataset.ack});await refresh()}catch(e){$('message').textContent=e.message}}}if(event.target.id==='clearFocus'){focused=null;servicePage=1;renderServices()}if(event.target.id==='testEmail'){event.target.disabled=true;try{await api('email-test',{});$('message').textContent='测试邮件已进入投递队列，请查看发送状态。';await refresh()}catch(e){$('message').textContent=e.message;event.target.disabled=false}}});
document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('circle[data-group]')){e.preventDefault();focusGroup(e.target.dataset.group)}});
$('rows').addEventListener('change',async e=>{if(e.target.dataset.op)await operate(e.target.dataset.id,e.target.dataset.op,e.target.checked)});
$('refresh').onclick=()=>refresh(true);$('openAdd').onclick=()=>{fillWebPreset(false);$('add').showModal()};['search','kind','state','serviceScope'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',()=>{servicePage=1;renderServices()}));$('rankMetric').onchange=ranking;$('trendMetric').onchange=()=>{historyFollow=true;trend()};$('historyCursor').oninput=()=>{historyFollow=Number($('historyCursor').value)===Number($('historyCursor').max);trend()};
$('create').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('.primary');button.disabled=true;try{const f=new FormData(e.target);const body={name:f.get('name'),cwd:f.get('cwd'),command:JSON.parse(f.get('command'))};if($('createType').value!=='custom')body.webAccess=$('createAccess').value;const result=await api('create',body);$('add').close();e.target.reset();$('formError').textContent='';$('message').textContent='已注册，启动后识别访问入口。'+(result.webBind?'监听地址：'+result.webBind:'');setView('services');await refresh()}catch(e){$('formError').textContent=e.message}finally{button.disabled=false}};
$('openRules').onclick=()=>{if(!data)return;const f=$('rulesForm');for(const [key,value] of Object.entries(data.rules)){const el=f.elements.namedItem(key);if(!el)continue;if(el.type==='checkbox')el.checked=value;else el.value=value}$('rules').showModal()};
$('rulesForm').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('.primary');button.disabled=true;try{const values={...data.rules};for(const el of e.target.elements){if(!el.name)continue;values[el.name]=el.type==='checkbox'?el.checked:Number(el.value)}await api('rules',values);$('rules').close();$('rulesError').textContent='';$('message').textContent='告警阈值已保存';await refresh()}catch(e){$('rulesError').textContent=e.message}finally{button.disabled=false}};
$('notification').onclick=async()=>{if(!('Notification' in window)){$('message').textContent='当前浏览器不支持桌面通知';return}const permission=await Notification.requestPermission();preference.set('localServiceNotifications',String(permission==='granted'));$('message').textContent=permission==='granted'?'已启用桌面通知（需要面板保持打开）；邮件由后台独立投递。':'通知未获授权，邮件告警仍会运行。'};
initialize();setInterval(()=>{if(!document.hidden)refresh()},10000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
function visibleServices(){if(!data)return [];if($('serviceScope').value!=='resources')return services;return data.resources.map(g=>({...g,nativeKind:g.kind,kind:'group',aliasId:g.id,pid:g.pids.length===1?g.pids[0]:null,running:true,ports:[...new Set(services.filter(s=>s.resourceId===g.id).flatMap(s=>s.ports))],resourceId:g.id,web:services.filter(s=>s.resourceId===g.id).flatMap(s=>s.web||[]),autostart:null,uptime:null,state:g.pids.length+' 个进程'}))}
async function loadNodes(){const result=await api('nodes');nodeList=result.nodes;if(!nodeList.some(n=>n.id===selectedNode))selectedNode=nodeList[0]?.id||'';$('nodeSelect').innerHTML=nodeList.map(n=>`<option value="${esc(n.id)}">${esc(n.name)}</option>`).join('')||'<option value="">尚未连接节点</option>';$('nodeSelect').value=selectedNode;renderNodes();if(!selectedNode){document.body.classList.add('offline-overlay');$('nodeStatus').textContent='尚未连接监控代理，请通过“管理节点”添加。';$('openAdd').disabled=true}}
function renderNodes(){$('nodeList').innerHTML=nodeList.map(n=>`<div class="node-row"><span><b>${esc(n.name)}</b><br><small>${esc(n.url)}</small></span>${n.id==='local'?'<small>内置</small>':`<div><button data-node-access="${esc(n.id)}">访问主机</button> <button data-remove-node="${esc(n.id)}">移除连接</button></div>`}</div>`).join('')||'<p>还没有节点。</p>'}
function setPalette(){const theme=document.documentElement.dataset.theme;colors.splice(0,colors.length,...(theme==='dark'?['#b3cc91','#9bbdd7','#b9a9ce','#d8b68a','#8fc1b5','#87949d']:['#527746','#3d729f','#80639f','#a06b34','#3e817c','#a0a196']))}
function openInfo(id){const item=[...visibleServices(),...services,...data.resources].find(s=>s.id===id);if(!item)return;aliasTarget=item.aliasId||item.id;$('infoTitle').textContent=display(item);$('infoRaw').textContent='原始标识：'+item.name;$('infoDescription').textContent=item.description||'尚无用途说明';$('infoFacts').textContent=(item.nameSource||'原始标识')+' · 节点 '+(data.node?.name||selectedNode)+(item.pids?' · PID '+item.pids.slice(0,40).join(', ')+(item.pids.length>40?' …':''):item.pid?' · PID '+item.pid:'');const form=$('aliasForm');form.elements.name.value=item.nameSource==='自定义'?item.displayName:'';form.elements.description.value=item.nameSource==='自定义'?item.description:'';$('aliasError').textContent='';$('serviceInfo').showModal()}
async function initialize(){setPalette();$('themeSelect').value=document.documentElement.dataset.theme||'comfort';$('fontSelect').value=preference.get('localServiceFont')||'15';const savedSort=preference.get('localServiceSort');if([...$('sort').options].some(o=>o.value===savedSort))$('sort').value=savedSort;sortLabel();try{await loadNodes();await refresh()}catch(e){$('message').textContent=e.message}}
$('themeSelect').onchange=()=>{document.documentElement.dataset.theme=$('themeSelect').value;preference.set('localServiceTheme',$('themeSelect').value);setPalette();render()};
$('fontSelect').onchange=()=>{document.documentElement.style.setProperty('--reading-size',$('fontSelect').value+'px');preference.set('localServiceFont',$('fontSelect').value)};
$('sort').onchange=()=>{sortAsc=$('sort').value==='name';servicePage=1;renderServices()};$('sortDirection').onclick=()=>{sortAsc=!sortAsc;servicePage=1;renderServices()};
$('nodeSelect').onchange=()=>{selectedNode=$('nodeSelect').value;preference.set('localServiceNode',selectedNode);data=null;services=[];focused=null;servicePage=1;notified=null;historyFollow=true;document.body.classList.add('offline-overlay');$('message').textContent='正在连接所选节点…';refresh(true)};
$('openNodes').onclick=async()=>{try{await loadNodes();$('nodesDialog').showModal()}catch(e){$('message').textContent=e.message}};
$('nodeForm').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{const form=new FormData(e.target);const result=await api('node-add',Object.fromEntries(form));selectedNode=result.id;preference.set('localServiceNode',selectedNode);e.target.reset();$('nodeError').textContent='';data=null;services=[];notified=null;focused=null;await loadNodes();$('nodesDialog').close();await refresh(true)}catch(e){$('nodeError').textContent=e.message}finally{button.disabled=false}};
$('aliasForm').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('.primary');button.disabled=true;try{const form=new FormData(e.target);await api('alias',{id:aliasTarget,name:form.get('name'),description:form.get('description')});$('serviceInfo').close();$('message').textContent='名称与说明已保存在当前节点';await refresh(true)}catch(e){$('aliasError').textContent=e.message}finally{button.disabled=false}};
document.addEventListener('click',async e=>{const sortButton=e.target.closest('[data-sort]');if(sortButton){servicePage=1;const field=sortButton.dataset.sort;if($('sort').value===field)sortAsc=!sortAsc;else{$('sort').value=field;sortAsc=field==='name'}renderServices()}const info=e.target.closest('[data-info]');if(info&&data)openInfo(info.dataset.info);const remove=e.target.closest('[data-remove-node]');if(remove&&confirm('移除此控制台的节点连接？远程采集与邮件告警仍会继续运行。')){try{await api('node-remove',{id:remove.dataset.removeNode});data=null;services=[];focused=null;notified=null;await loadNodes();await refresh(true)}catch(error){$('message').textContent=error.message}}});

let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(trend,150)});

function renderActiveView(){if(!data)return;({overview:()=>{donut('cpuDonut','cpu');donut('memoryDonut','memory');ranking();trend()},services:renderServices,network:renderNetwork,storage:renderStorage,security:renderSecurity,alerts:renderAlerts})[currentView]?.()}

$('storageScope').onchange=()=>{preference.set('storageScope:'+selectedNode,$('storageScope').value);renderStorageHeader();renderStorage()};
$('servicePrevious').onclick=()=>{servicePage--;renderServices()};
$('serviceNext').onclick=()=>{servicePage++;renderServices()};
let webTarget=null;
function webUrl(scheme,host,port){return scheme+'://'+(host.includes(':')?'['+host+']':host)+':'+port+'/'}
function loopback(address){return address==='::1'||address.startsWith('127.')||address==='localhost'}
function webLinks(entry){
 const context=data.accessContext||{},local=context.viewerLocal===true,lan=data.webAccess?.lanAddresses||[];
 const links=[];if(entry.customUrl)links.push({label:'自定义访问',url:entry.customUrl});
 if(local){links.push({label:(loopback(entry.probeHost)?'访问 :':'局域网访问 :')+entry.port,url:webUrl(entry.scheme,entry.probeHost,entry.port)});}
 else if(!loopback(entry.address)){
  const host=context.accessHost||(!['*','0.0.0.0','::'].includes(entry.address)?entry.address:'');
  if(host)links.push({label:'节点访问 :'+entry.port,url:webUrl(entry.scheme,host,entry.port)});
 }
 if(entry.publication)links.push({label:'局域网 :'+entry.publication.port,url:webUrl(entry.scheme,entry.publication.host,entry.publication.port)});
 else if(!loopback(entry.address)){
  const host=['*','0.0.0.0','::'].includes(entry.address)?lan[0]:lan.includes(entry.address)?entry.address:null;
  if(host)links.push({label:'局域网 :'+entry.port,url:webUrl(entry.scheme,host,entry.port)});
 }
 return links.filter((l,i,a)=>a.findIndex(x=>x.url===l.url)===i);
}
function renderWebLinks(service){
 const entries=service.web||[];if(!entries.length)return '<small>—</small>';
 const unique=entries.filter((e,i,a)=>a.findIndex(x=>x.key===e.key)===i);
 const links=unique.flatMap(webLinks).filter((l,i,a)=>a.findIndex(x=>x.url===l.url)===i);
 return '<div class="web-links">'+links.slice(0,4).map(l=>`<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer" title="${esc(l.url)}">↗ ${esc(l.label)}</a>`).join('')+
 (!links.length?'<small>仅节点本机，需转发或配置域名</small>':'')+
 `<button data-web-info="${esc(service.id)}">访问设置${unique.length>1?' ('+unique.length+'端口)':''}</button></div>`;
}
function fillWebPreset(force){
 if(!data)return;const type=$('createType').value;const command=$('serviceCommand');
 if(force||!command.value){
  const exe=data.capabilities.pythonExecutable||'/usr/bin/python3';
  if(type==='python')command.value=JSON.stringify([exe,'-u','-m','http.server','8000','--bind',($('createAccess').value==='lan'&&data.webAccess?.lanAddresses?.[0])||'127.0.0.1']);
  else if(force&&type==='vite')command.value=JSON.stringify(['/absolute/path/to/node','/absolute/path/to/vite/bin/vite.js','--port','5173','--host','127.0.0.1']);
  else if(force&&type==='next')command.value=JSON.stringify(['/absolute/path/to/node','/absolute/path/to/next/dist/bin/next','dev','--port','3000','--hostname','127.0.0.1']);
 }
 $('createAccess').disabled=type==='custom';
 const lan=data.webAccess?.lanAddresses||[];
 $('createAccessInfo').textContent=type==='custom'?'普通命令保留自身网络配置。':$('createAccess').value==='local'?'仅监听 127.0.0.1。':lan.length?'启动时默认监听 '+lan[0]+'，不使用 0.0.0.0。':'当前未发现局域网地址，将回退到 127.0.0.1。';
}
function currentWeb(){return webTarget?.entries.find(e=>e.key===$('webEndpoint').value)}
function updateWebDialog(){
 const entry=currentWeb();if(!entry)return;const form=$('webForm');form.elements.url.value=entry.customUrl||'';
 const links=webLinks(entry);$('webCurrent').textContent='监听 '+entry.address+':'+entry.port+' · '+entry.scheme.toUpperCase()+
 (links.length?' · '+links.map(l=>l.url).join(' / '):' · 浏览器不能直接使用远程节点的 127.0.0.1');
 const canPublish=data.capabilities.control&&loopback(entry.address)&&data.webAccess?.lanAddresses.length;
 $('publishLan').disabled=!canPublish;$('publishLan').checked=Boolean(entry.publicationConfigured||entry.publication);
 $('publishPort').value=entry.publication?.port||0;$('publishPort').disabled=!canPublish;
 $('publishNote').textContent=!data.capabilities.control?'此节点仅监控，未开放局域网发布控制。':!loopback(entry.address)?'当前服务已监听网络地址，优先使用直连链接。':
 !data.webAccess?.lanAddresses.length?'没有可用的局域网地址。':'仅绑定 '+data.webAccess.lanAddresses[0]+'。0 自动分配端口；原应用的登录、域名、回调限制仍由应用控制。';
 if(entry.publicationError)$('publishNote').textContent+=' '+entry.publicationError;
}
function openWeb(id){
 const service=[...visibleServices(),...services].find(s=>s.id===id);if(!service?.web?.length)return;
 webTarget={id,entries:service.web.filter((e,i,a)=>a.findIndex(x=>x.key===e.key)===i)};
 $('webTitle').textContent=display(service);$('webEndpoint').innerHTML=webTarget.entries.map(e=>`<option value="${esc(e.key)}">${e.scheme.toUpperCase()} ${e.address}:${e.port}</option>`).join('');
 $('webError').textContent='';updateWebDialog();$('webDialog').showModal();
}
$('createType').onchange=()=>fillWebPreset(true);$('createAccess').onchange=()=>fillWebPreset(false);$('webEndpoint').onchange=updateWebDialog;
$('webForm').onsubmit=async event=>{
 event.preventDefault();const entry=currentWeb();if(!entry)return;const button=event.target.querySelector('.primary');button.disabled=true;let saved=false;
 try{
  await api('web-link',{key:entry.key,url:event.target.elements.url.value});saved=true;
  const enabled=$('publishLan').checked,old=Boolean(entry.publicationConfigured||entry.publication);
  const port=Number($('publishPort').value);
  if(!$('publishLan').disabled&&(enabled!==old||enabled&&port!==entry.publication?.port))await api('web-publish',{key:entry.key,enabled,port,pid:entry.pid,created:entry.created});
  $('webDialog').close();$('message').textContent='访问设置已保存；入口链接已更新。';await refresh(true);
 }catch(e){$('webError').textContent=(saved?'地址已保存，局域网设置未完成：':'')+e.message}
 finally{button.disabled=false}
};
document.addEventListener('click',async event=>{
 const web=event.target.closest('[data-web-info]');if(web&&data)openWeb(web.dataset.webInfo);
 const node=event.target.closest('[data-node-access]');if(node){
  const entry=nodeList.find(n=>n.id===node.dataset.nodeAccess);if(!entry)return;
  const host=prompt('浏览器访问此节点时使用的 IP 或域名（不含协议、端口；留空清除）：',entry.accessHost||'');
  if(host===null)return;
  try{await api('node-access',{id:entry.id,accessHost:host});await loadNodes();await refresh(true);$('message').textContent='节点访问主机已保存'}catch(e){$('message').textContent=e.message}
 }
});
