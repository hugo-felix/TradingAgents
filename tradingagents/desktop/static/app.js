'use strict';
(() => {
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {today:'今日关注',research:'股票研究',holdings:'持仓管理',history:'历史记录',stock:'股票档案',analysis:'分析任务',settings:'应用设置'};
  const ratings = {Buy:'买入',Overweight:'增配',Hold:'持有',Underweight:'减配',Sell:'卖出',REVIEW:'需复核'};
  const actions = {observe:'继续观察',review:'安排重新研究',change:'调整个人计划'};
  const tabs = {summary:'研究摘要',evidence:'事实与依据',judgment:'我的判断',full:'完整报告'};
  const state = {page:'today',tab:'summary',section:'decision',selected:null,preview:null,previewError:'',previewScroll:0,report:null,reports:[],allReports:[],pending:[],pendingOffset:0,stockRecords:[],decisionOffset:0,holdings:[],records:[],settings:{},providers:[],task:{status:'idle'},query:'',rating:'',offset:0,historyOffset:0,historyKind:'',historyTicker:'',historyItems:[],stack:[],drafts:{},theme:'dark',busy:false,library:{status:'starting',revision:0,directories:[],errors:[]}};
  let bridge, pollTimer, toastTimer, searchTimer, loadingReport=0, seenTaskStatus='', seenLibraryRevision=0, previewToken=0;
  const dateText = (value) => String(value || '').replace('T',' ').slice(0,19);
  const badge = (rating) => `<span class="badge ${rating==='REVIEW'?'review':'gold'}">${esc(ratings[rating] || rating)}</span>`;
  const reading = item => item?.display_text ?? item?.text ?? '';
  const sourceStates={available:'已取得',unavailable:'不可用',missing:'缺失',unverified:'未核实',failed:'未通过',passed:'已通过',skipped:'已跳过',error:'异常',empty:'无返回内容',withheld:'暂不提供',unknown:'未知'};
  const nameOf = (r) => esc(r.name || r.ticker);
  const sourceButton = (item, label='原文依据') => item ? `<button class="link source-action" data-source="${esc(item.source)}">${label}<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg></button>` : '';
  function toast(message, error=false) { const el=$('#toast');el.textContent=message;el.classList.toggle('error',error);el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,error?9000:5000); }
  async function api(method,...args) {
    if (!bridge) throw new Error('请通过桌面应用打开工作台。');
    const response = await bridge[method](...args);
    if (!response || !response.ok) throw new Error(response?.error || '操作未完成，请重试。');
    return response.data;
  }
  function taskActive() {return ['running','cancelling'].includes(state.task.status);}
  function rememberDrafts() { document.querySelectorAll('[data-draft]').forEach(el=>state.drafts[el.dataset.draft]=el.value); }
  function restoreDrafts() { document.querySelectorAll('[data-draft]').forEach(el=>{if(Object.hasOwn(state.drafts,el.dataset.draft))el.value=state.drafts[el.dataset.draft];}); }
  function snapshot() {rememberDrafts();return {page:state.page,tab:state.tab,section:state.section,selected:state.selected,preview:state.preview,previewScroll:$('#preview-scroll')?.scrollTop||0,report:state.report,query:state.query,rating:state.rating,offset:state.offset,stockRecords:state.stockRecords,decisionOffset:state.decisionOffset,pendingOffset:state.pendingOffset,pending:state.pending,historyItems:state.historyItems,historyOffset:state.historyOffset,historyTicker:state.historyTicker,historyKind:state.historyKind,decisionOpen:!!document.querySelector('#decision-form:not([hidden])'),scroll:window.scrollY};}
  function go(page,root=false) {
    if (!Object.hasOwn(labels,page)) return;
    if (page===state.page && !root) return;
    ++loadingReport;rememberDrafts();if(root)state.stack=[];else state.stack.push(snapshot());
    state.page=page;render();window.scrollTo(0,0);
    if(page==='history')loadHistory();
  }
  async function openReport(id) {
    const token=++loadingReport;
    const report=await api('get_report',id);
    if(token!==loadingReport)return false;
    const stockRecords=await api('history',report.ticker,'decision',0);
    if(token!==loadingReport)return false;
    state.stack.push(snapshot());state.stockRecords=stockRecords;state.decisionOffset=0;state.report=report;state.page='stock';state.tab='summary';state.section='decision';render();window.scrollTo(0,0);return true;
  }
  function render() {
    $('#page-label').textContent=labels[state.page];$('#back').hidden=state.stack.length===0;
    document.querySelectorAll('.sidebar [data-page]').forEach(b=>{if(b.dataset.page===state.page||(state.page==='stock'&&b.dataset.page==='research'))b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    const views={today:today,research:research,holdings:holdings,history:history,stock:stock,analysis:analysis,settings:settings};
    window.StockSeekControls?.close();$('#content').innerHTML=views[state.page]();restoreDrafts();window.StockSeekControls?.enhance($('#content'));document.querySelectorAll('form').forEach(form=>form.noValidate=true);const stockInput=$('#analysis-form [name="ticker"]');if(stockInput?.value)resolveStockField(stockInput);renderTask();if(state.page==='today'){fitPreview();const scroller=$('#preview-scroll');if(scroller)scroller.scrollTop=state.previewScroll||0;ensurePreview();}
  }
  function today() {
    const updates=state.pending;
    const selected=updates.find(r=>r.id===state.selected)||updates[0];
    const date=new Intl.DateTimeFormat('zh-CN',{month:'long',day:'numeric',weekday:'long'}).format(new Date());
    return `<div class="page-head"><div><h1>今日关注</h1><p>${esc(date)}</p></div><div class="page-meta" data-monitoring-status><span class="status-dot"></span><span>本地报告 · 自动更新</span><details class="scope-popover"><summary aria-label="查看数据覆盖范围">数据范围</summary><p>自动读取本机研究报告。行情与公告尚未持续监测。</p></details></div></div>
      ${state.allReports.length||updates.length?`<div class="section-head workspace-heading"><h2>研究更新 <span class="count">${updates.length===100?'100+':updates.length}</span></h2><span class="muted">按分析日期排序</span></div><div class="split"><div class="queue">${updates.length?updates.map(r=>`<button class="queue-row" data-inspect="${esc(r.id)}" aria-pressed="${r.id===selected?.id}"><div class="queue-identity"><div><strong>${nameOf(r)}</strong><span class="stock-code">${esc(r.ticker)}</span></div>${badge(r.rating)}</div><p class="preview-text">${r.rating==='REVIEW'?'报告需复核':r.has_previous?'研究已更新，查看各维度重点':'首次研究，查看各维度重点'}</p><div class="queue-foot"><time>${esc(r.date)}</time><span>查看研究 →</span></div></button>`).join(''):`<div class="empty"><h3>研究均已留下判断</h3><p class="muted">可在股票研究中回看。</p></div>`}</div>
      ${selected?`<section id="today-detail" class="inspector research-preview" aria-label="研究速览"><header class="preview-header"><div class="preview-heading"><span>研究速览</span><time>截至 ${esc(selected.date)}</time></div><div class="preview-stock"><h2>${nameOf(selected)} <span class="muted">${esc(selected.ticker)}</span></h2>${badge(selected.rating)}</div></header><div id="preview-scroll" class="preview-scroll" tabindex="0" aria-label="速览内容，可滚动">${previewContent(selected)}</div><footer class="preview-actions"><div class="actions"><button class="primary" data-report="${esc(selected.id)}">打开研究</button><button data-report="${esc(selected.id)}" data-judgment="true">记录判断</button></div><span>原文摘录 · 点击重点查看依据</span></footer></section>`:''}</div>`:
      `<section class="onboarding"><svg class="empty-icon" viewBox="0 0 48 48" aria-hidden="true"><path d="M8 12h13l4 5h15v22H8zM24 23v10m-4-4 4 4 4-4"/></svg><h2>${['starting','scanning'].includes(state.library.status)?'正在读取本地研究':'开始你的第一份研究'}</h2><p>${['starting','scanning'].includes(state.library.status)?'已有报告会自动出现在这里。':'选择股票，生成研究摘要与完整依据。'}</p><div class="actions"><button class="primary" data-page="analysis">新建研究</button><button class="quiet" data-action="import">读取其他报告目录</button></div></section>`}
      ${state.pendingOffset||updates.length===100?`<div class="pagination"><button data-action="pending-prev" ${state.pendingOffset===0?'disabled':''}>上一页</button><span>第 ${state.pendingOffset/100+1} 页</span><button data-action="pending-next" ${updates.length<100?'disabled':''}>下一页</button></div>`:''}`;
  }
  function research() {
    return `<div class="page-head"><div><h1>股票研究</h1><p>股票档案与研究版本</p></div></div>
      <div class="tools"><div class="search-field"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg><input class="search" id="report-search" type="search" placeholder="搜索名称或代码" aria-label="搜索名称或代码" value="${esc(state.query)}"></div><div class="actions"><select id="rating-filter" aria-label="评级筛选"><option value="">全部评级</option>${Object.entries(ratings).map(([k,v])=>`<option value="${k}" ${state.rating===k?'selected':''}>${v}</option>`).join('')}</select><button data-action="refresh-reports" class="quiet">刷新</button></div></div>
      ${state.reports.length?`<div class="table-wrap"><table class="research-table"><thead><tr><th>股票</th><th>最新观点</th><th>结论摘录</th><th>分析日期</th><th></th></tr></thead><tbody>${state.reports.map(r=>`<tr><td><button class="link stock-name" data-report="${esc(r.id)}"><strong>${nameOf(r)}</strong></button><small>${esc(r.ticker)}${state.holdings.some(h=>h.ticker===r.ticker)?' · 持仓':''}</small></td><td>${badge(r.rating)}</td><td class="table-conclusion"><span class="clamp">${esc(r.display_conclusion||r.conclusion||'暂无独立摘要')}</span></td><td class="table-date">${esc(r.date)}<small>版本 ${esc(r.id.slice(0,8))}</small></td><td><button class="quiet row-open" data-report="${esc(r.id)}" aria-label="打开 ${esc(r.name||r.ticker)} 的研究">→</button></td></tr>`).join('')}</tbody></table></div>`:`<div class="empty"><h2>${state.query||state.rating?'没有匹配的研究':'还没有研究报告'}</h2><p class="muted">${state.query||state.rating?'调整搜索条件后重试。':'新建研究后会自动保存在这里。'}</p></div>`}
      ${state.offset||state.reports.length===100?`<div class="pagination"><button data-action="reports-prev" ${state.offset===0?'disabled':''}>上一页</button><span>第 ${state.offset/100+1} 页</span><button data-action="reports-next" ${state.reports.length<100?'disabled':''}>下一页</button></div>`:''}`;
  }
  function excerpt(item, fallback='原报告未单独提供。') {
    return item?`<p class="clamp">${esc(reading(item))}</p>${sourceButton(item)}`:`<p class="muted">${fallback}</p>`;
  }
  function stock() {
    const r=state.report;if(!r)return '<div class="empty">请先选择一份研究报告。</div>';
    const holding=state.holdings.find(h=>h.ticker===r.ticker);
    return `<div class="page-head"><div><div class="report-title"><h1>${nameOf(r)}</h1>${r.name&&r.name!==r.ticker?`<span class="muted">${esc(r.ticker)}</span>`:''}${badge(r.rating)}</div><p class="report-meta"><time>${esc(r.date)}</time><span>版本 ${esc(r.id.slice(0,8))}</span>${holding?`<button class="link" data-page="holdings">持仓 ${holding.quantity.toLocaleString()} 股</button>`:''}</p></div><div class="actions"><button data-action="export" class="quiet">导出</button><button class="primary" data-action="reanalyze">更新研究</button></div></div>
      ${r.warnings.length?`<details class="quality-bar ${r.rating==='REVIEW'?'needs-review':''}"><summary><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2 21h20zM12 9v5m0 3v1"/></svg><span>${r.rating==='REVIEW'?'报告需复核':'数据核查'}</span><span class="muted">${r.warnings.length} 项说明</span><span class="quality-expand">查看详情</span></summary><ul>${r.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>`:''}
      <div class="tabs" role="tablist" aria-label="研究档案">${Object.entries(tabs).map(([k,v])=>`<button role="tab" id="tab-${k}" aria-controls="stock-panel" aria-selected="${state.tab===k}" tabindex="${state.tab===k?0:-1}" data-tab="${k}">${v}</button>`).join('')}</div>
      <div id="stock-panel" role="tabpanel" aria-labelledby="tab-${state.tab}">${({summary:summary,evidence:evidence,judgment:judgment,full:full})[state.tab](r)}</div>`;
  }
  function summary(r) {
    const s=r.summary;
    return `<div class="summary-workspace"><div class="summary-main"><section class="conclusion"><div class="section-kicker"><span>研究结论</span><span class="muted">模型观点 · ${esc(r.date)}</span></div>${s.conclusion?`<p class="quote">${esc(reading(s.conclusion))}</p>${sourceButton(s.conclusion,'完整执行摘要')}`:`<p class="quote">暂无结构化摘要</p><button class="link" data-tab="full">查看最终决策</button>`}</section>
      ${decisionPlan(r)}<section class="thesis"><div class="section-head"><h2>核心依据</h2></div>${s.reasons.length?s.reasons.map((item,i)=>`<div class="argument"><span class="number">${String(i+1).padStart(2,'0')}</span><div><p>${esc(reading(item).replace(/^\d+[.)、]\s*/,''))}</p></div>${sourceButton(item,'依据')}</div>`).join(''):'<p class="muted">暂无可提取论据，可在完整报告中查看。</p>'}</section>
      ${r.previous?`<section class="version-changes"><div class="section-head"><h2>较上次研究</h2><button class="link" data-report="${esc(r.previous.id)}">${esc(r.previous.date)} 版本 →</button></div>${r.changes.length?r.changes.map(c=>`<div class="comparison"><strong>${esc(c.label)}</strong><p class="before clamp">上次 ${esc(c.display_before??c.before)}</p><p class="after clamp">本次 ${esc(c.display_after??c.after)}</p></div>`).join(''):'<p class="muted">评级、结论摘录与判断条件未变化。</p>'}</section>`:`<div class="first-version"><span class="status-dot"></span>首次研究<span>暂无历史版本可比较</span></div>`}</div>
      <aside class="summary-aside"><section class="side-note"><h2>反向证据与风险</h2>${excerpt(s.counter)}</section><section class="side-note"><h2>重新评估条件</h2>${excerpt(s.conditions)}</section><div class="summary-context"><span>当前版本</span><strong>${esc(r.id.slice(0,8))}</strong><p>${esc(r.date)} 的研究快照</p></div></aside></div>`;
  }
  function evidence(r) {
    const section=r.sections.find(s=>s.id===state.section)||r.sections.find(s=>s.id==='decision')||r.sections[0];
    return `
      <details><summary>数据来源与校验记录（${r.coverage.length} 项来源）</summary>${r.coverage.length?`<div class="table-wrap"><table><thead><tr><th>引用</th><th>工具</th><th>可用状态</th></tr></thead><tbody>${r.coverage.map(c=>`<tr><td>${esc(c.reference)}</td><td>${esc(c.tool)}</td><td>${esc(sourceStates[c.status]||'未知')}${c.html?`<details><summary>原始返回内容</summary><div class="source-block">${c.html}</div></details>`:''}</td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">此报告没有可用的来源清单。保留的段落只作为模型原文，不冒充独立事实验证。</p>'}</details>
      <div class="reader-layout"><nav class="reader-nav" aria-label="依据主题">${r.sections.filter(s=>!s.debate).map(s=>`<button data-section="${esc(s.id)}" aria-pressed="${s.id===section?.id}">${esc(s.title)}</button>`).join('')}</nav><div class="reader panel"><div class="section-head"><h2>${esc(section?.title||'原文依据')}</h2><span class="muted">${esc(r.date)}</span></div>${(section?.blocks||[]).map(block=>`<div class="source-block" id="${esc(block.id)}">${block.display_html||block.html}</div>`).join('')}</div></div>`;
  }
  function judgment(r) {
    const records=state.stockRecords;
    return `<div class="summary-layout"><div><div class="panel"><div class="section-head"><h2>我的研究判断</h2><button data-action="toggle-decision">记录新判断</button></div><p class="muted">模型观点：${esc(ratings[r.model_rating]||r.model_rating)} · ${esc(r.date)}。个人计划与实际持仓分别维护。</p><form id="decision-form" hidden><label>处理方式<select name="action" data-draft="decision-action-${esc(r.id)}">${Object.entries(actions).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label><label>判断依据<textarea name="reason" data-draft="decision-reason-${esc(r.id)}" required maxlength="10000" placeholder="哪些依据仍成立？哪些需要重新核查？"></textarea></label><div class="actions"><button type="submit" class="primary">保存个人判断</button><button type="button" data-action="toggle-decision">暂不记录</button></div><p class="muted">“安排重新研究”会保留你的研究意图，需要由你启动实际分析。</p></form></div><div class="timeline">${records.length?records.map(recordHTML).join(''):'<p class="muted">尚未记录个人判断。没有新的想法时可以跳过。</p>'}</div><div class="pagination"><button data-action="decisions-prev" ${state.decisionOffset===0?'disabled':''}>上一页判断</button><span>第 ${state.decisionOffset/100+1} 页</span><button data-action="decisions-next" ${records.length<100?'disabled':''}>下一页判断</button></div></div><div class="panel side-note"><h2>这次判断对应的研究</h2><div class="row"><span>分析日期</span><span>${esc(r.date)}</span></div><div class="row"><span>模型评级</span><span>${esc(ratings[r.rating])}</span></div><div class="row"><span>报告版本</span><span>${esc(r.id.slice(0,8))}</span></div><p class="muted">保存判断不会更新股数、成本或自动下单。</p></div></div>`;
  }
  function full(r) {
    const section=r.sections.find(s=>s.id===state.section)||r.sections[0];
    return `<div class="tools"><span class="muted">${r.character_count.toLocaleString()} 字符 · ${r.sections.length} 个章节</span><div class="actions"><input id="reader-search" type="search" placeholder="查找当前章节" aria-label="查找当前章节"><button data-action="reader-find">查找</button><button data-action="export">导出 Markdown</button></div></div><div class="reader-layout"><nav class="reader-nav" aria-label="完整报告目录">${r.sections.filter(s=>!s.debate).map(s=>`<button data-section="${esc(s.id)}" aria-pressed="${section?.id===s.id}">${esc(s.title)}</button>`).join('')}<details><summary>辩论与风控过程</summary>${r.sections.filter(s=>s.debate).map(s=>`<button data-section="${esc(s.id)}" aria-pressed="${section?.id===s.id}">${esc(s.title)}</button>`).join('')}</details></nav><div class="reader panel"><div class="section-head"><h2>${esc(section?.title||'报告')}</h2><span class="muted">原始正文</span></div>${(section?.blocks||[]).map(block=>`<div class="source-block" id="${esc(block.id)}">${block.display_html||block.html}</div>`).join('')}</div></div>`;
  }
  function holdings() {
    return `<div class="page-head"><div><h1>持仓管理</h1><p>实际持仓快照</p></div><button class="primary" data-action="new-holding">＋ 录入持仓</button></div><div class="table-caption">数量与成本来自手工记录 · 行情、现金与分红流水未接入</div>${state.holdings.length?`<div class="table-wrap"><table><thead><tr><th>股票</th><th>实际数量</th><th>每股成本</th><th>最近研究</th><th></th></tr></thead><tbody>${state.holdings.map(h=>{const r=h.report_id?{id:h.report_id,date:h.report_date,rating:h.report_rating}:null;return `<tr><td><strong>${nameOf(h)}</strong><small>${esc(h.ticker)}</small></td><td>${h.quantity.toLocaleString()} 股</td><td>${Number(h.cost).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:4})} 元</td><td>${r?`<button class="link" data-report="${esc(r.id)}">${esc(r.date)} · ${esc(ratings[r.rating])}</button>`:'尚无研究'}</td><td><button data-holding="${esc(h.ticker)}">更新快照</button></td></tr>`;}).join('')}</tbody></table></div>`:`<div class="panel empty"><h2>尚未录入实际持仓</h2><p class="muted">没有持仓也可以使用研究功能。录入后，各股票档案会显示持仓上下文。</p><div class="actions"><button data-action="new-holding">录入第一笔持仓</button></div></div>`}`;
  }
  function recordHTML(item) {
    return `<article class="record"><div class="record-title"><strong>${nameOf(item)}</strong><span class="record-code">${esc(item.ticker)}</span><span class="badge">${item.kind==='holding'?'实际持仓变化':esc(actions[item.action]||'个人判断')}</span></div><p>${esc(reading(item))}</p><time>${esc(dateText(item.time))}</time>${item.report_id?` · <button class="link" data-report="${esc(item.report_id)}">查看当时研究</button>`:''}</article>`;
  }
  function history() {
    return `<div class="page-head"><div><h1>历史记录</h1><p>研究版本、个人判断与持仓变化</p></div></div><div class="tools"><div class="actions"><input id="history-ticker" placeholder="股票名称或代码" value="${esc(state.historyTicker)}" aria-label="历史股票名称或代码"><select id="history-kind" aria-label="记录类型"><option value="">全部类型</option><option value="research" ${state.historyKind==='research'?'selected':''}>研究版本</option><option value="decision" ${state.historyKind==='decision'?'selected':''}>个人判断</option><option value="holding" ${state.historyKind==='holding'?'selected':''}>实际持仓</option></select><button data-action="filter-history">筛选</button></div></div><div class="timeline">${state.historyItems.map(item=>item.kind==='research'?`<article class="record"><div class="record-title"><strong>${nameOf(item)}</strong><span class="record-code">${esc(item.ticker)}</span><span class="badge">模型研究</span>${badge(item.rating)}</div><p><button class="link" data-report="${esc(item.report_id)}">查看研究版本 ${esc(item.id.slice(0,8))} →</button></p><time>分析日期 ${esc(item.time)}</time></article>`:recordHTML(item)).join('')||'<div class="panel empty"><h2>暂无匹配记录</h2><p class="muted">导入报告、保存判断或更新实际持仓后会在这里留档。</p></div>'}</div><div class="pagination"><button data-action="history-prev" ${state.historyOffset===0?'disabled':''}>上一页记录</button><span>第 ${state.historyOffset/100+1} 页</span><button data-action="history-next" ${state.historyItems.length<100?'disabled':''}>下一页记录</button></div>`;
  }
  function modelFields(prefix) {
    const s=state.settings;
    return `<div class="form-grid"><label>模型服务商<select name="provider" id="${prefix}-provider" data-draft="${prefix}-provider">${state.providers.map(p=>`<option value="${esc(p)}" ${s.provider===p?'selected':''}>${esc(p)}</option>`).join('')}</select></label><label>自定义服务地址（可选）<input name="backend_url" data-draft="${prefix}-url" value="${esc(s.backend_url||'')}" type="url" placeholder="默认使用服务商地址"></label></div><div class="form-grid"><label>快速模型 ID<input name="quick_model" data-draft="${prefix}-quick" value="${esc(s.quick_model||'')}" required maxlength="160"></label><label>深度模型 ID<input name="deep_model" data-draft="${prefix}-deep" value="${esc(s.deep_model||'')}" required maxlength="160"></label></div>`;
  }
  function analysis() {
    return `<div class="page-head"><div><h1>分析任务</h1><p>运行中的研究与任务记录</p></div></div><div class="panel form-panel"><form id="analysis-form"><div class="form-grid"><label class="stock-field">股票代码<input name="ticker" data-draft="analysis-ticker" placeholder="601658 / 600941 / AAPL" required maxlength="40" autocomplete="off" aria-describedby="analysis-stock-match"><div id="analysis-stock-match" class="stock-match" role="status" aria-live="polite">输入代码后自动识别名称与市场</div></label><label>分析日期<input name="date" data-draft="analysis-date" type="date" value="${localDate()}" max="${localDate()}" required></label></div>${modelFields('analysis')}<p class="muted">使用配置服务商的 API，会产生相应费用。凭据从环境变量或系统凭据库读取；新结果单独保存。</p><div class="actions"><button class="primary" type="submit" ${taskActive()?'disabled':''}>开始研究</button><button type="button" data-page="settings">配置凭据</button></div></form></div><div id="task-detail" class="panel form-panel">${taskDetail()}</div>`;
  }
  function taskDetail() {
    const t=state.task;
    if(t.status==='idle')return '<h3>尚无分析任务</h3><p class="muted">运行后会显示真实阶段与耗时，不估算虚假完成百分比。</p>';
    return `<span class="eyebrow">${esc(t.ticker)} · ${esc(t.date)}</span><div class="task-detail"><strong>${esc(t.phase||t.status)}</strong><span class="muted">${elapsed(t)}${t.error_kind?` · ${esc(t.error_kind)}`:''}</span></div><div class="actions">${taskActive()?`<button data-action="cancel" ${t.status==='cancelling'?'disabled':''}>取消分析</button>`:''}${t.status==='completed'?`<button class="primary" data-report="${esc(t.report_id)}">查看新研究</button>`:''}${['failed','cancelled','interrupted'].includes(t.status)?'<button data-action="retry-task">按原配置重试</button>':''}</div>`;
  }
  function settings() {
    return `<div class="page-head"><div><h1>应用设置</h1><p>模型、凭据与本地存储</p></div></div><div class="panel form-panel"><h2>分析模型默认值</h2><form id="settings-form">${modelFields('settings')}<button class="primary" type="submit">保存默认模型</button></form></div><div class="panel form-panel"><h2>API 凭据</h2><p class="muted">保存在 macOS 钥匙串或 Windows 凭据库。不会写入报告或工作台数据库。也支持已有环境变量与启动目录中的 .env。</p><form id="credential-form"><label>服务商<select name="provider" id="credential-provider">${state.providers.map(p=>`<option value="${esc(p)}" ${p===state.settings.provider?'selected':''}>${esc(p)}</option>`).join('')}</select></label><p id="credential-status" class="muted">点击检查凭据状态。</p><label>API Key<input name="key" type="password" autocomplete="off" spellcheck="false" placeholder="仅输入新凭据时填写" required></label><div class="actions"><button type="submit">保存到系统凭据库</button><button type="button" data-action="credential-check">检查配置</button></div></form><details><summary>本地模型与订阅登录</summary><p class="muted">Ollama 使用本地服务地址和已安装的模型 ID。ChatGPT 使用现有 CLI 登录状态：先运行 tradingagents login chatgpt。Bedrock / Azure 等企业服务仍需相应环境配置。</p></details></div><div class="panel form-panel"><h2>报告自动读取</h2><p class="muted">启动时读取已有报告，每 15 秒检查新增或变更。选择过的目录会保留。</p><div class="report-directories">${state.library.directories.length?state.library.directories.map(path=>`<p class="muted prewrap">${esc(path)}</p>`).join(''):'<p class="muted">当前没有关联目录。</p>'}</div>${state.library.errors.length?`<p class="muted">${state.library.errors.length} 项读取异常，下次检查自动重试。</p>`:''}<div class="actions"><button data-action="import">添加报告目录</button><button data-action="refresh-reports">立即检查</button></div></div><div class="panel form-panel"><h2>本地数据</h2><p class="muted prewrap">${esc(state.data_directory)}</p><p class="muted">包含报告快照、个人记录、任务和运行缓存。备份时关闭应用后复制整个目录。导出报告可在股票档案中操作。</p><div class="actions"><button data-action="theme">切换${state.theme==='dark'?'浅色':'深色'}主题</button></div></div>`;
  }
  function localDate() {const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
  function elapsed(t) {if(!t.started_epoch)return '';const end=t.finished_at?new Date(t.finished_at).getTime():Date.now();const secs=Math.max(0,Math.floor(end/1000-t.started_epoch));return `耗时 ${Math.floor(secs/60)} 分 ${secs%60} 秒`;}
  function renderTask() {
    const t=state.task,el=$('#task-strip');el.hidden=t.status==='idle';el.classList.toggle('running',t.status==='running');
    document.documentElement.style.setProperty('--task-height','0px');
    if(el.hidden){fitPreview();return;}
    el.innerHTML=`<span><span class="task-dot"></span>${esc(t.ticker)} · ${esc(t.phase||t.status)} <span class="muted">${elapsed(t)}</span></span><div class="actions">${t.status==='completed'?`<button data-report="${esc(t.report_id)}">查看结果</button>`:''}<button data-page="analysis">任务详情</button>${t.status==='running'?'<button data-action="cancel">取消</button>':''}</div>`;
    document.documentElement.style.setProperty('--task-height',`${el.offsetHeight}px`);
    fitPreview();
    if(state.page==='analysis'&&$('#task-detail'))$('#task-detail').innerHTML=taskDetail();
  }
  async function refreshData() {
    const data=await api('bootstrap');Object.assign(state,{library:data.library,pending:data.pending,allReports:data.reports,holdings:data.holdings,records:data.records,task:data.task,settings:data.settings,providers:data.providers,data_directory:data.data_directory});
    state.reports=await api('list_reports',state.query,state.rating,state.offset);
    if(state.report)state.stockRecords=await api('history',state.report.ticker,'decision',state.decisionOffset);
    if(state.pendingOffset)state.pending=await api('pending_reports',state.pendingOffset);
  }
  async function loadReports() {
    state.reports=await api('list_reports',state.query,state.rating,state.offset);if(state.page==='research')render();
  }
  async function loadHistory() {
    state.historyItems=await api('history_entries',state.historyTicker,state.historyKind,state.historyOffset);
    if(state.page==='history')render();
  }
  const angleStates={ready:'重点',not_run:'未提供',review:'需复核',unstructured:'未提取',insufficient:'数据不足',unassessed:'未评估'};
  function decisionPlan(report,preview=false){
    const fields=report.preview?.decision_plan||[];
    if(!fields.length)return '';
    const item=field=>`<div class="decision-field" data-plan-field="${esc(field.key)}"><h4>${esc(field.label)}</h4>${field.item?`<button class="${preview?'preview-point':'link plan-source'}" ${preview?`data-preview-source="${esc(field.item.source)}" data-report-id="${esc(report.id)}"`:`data-source="${esc(field.item.source)}"`} title="查看原文依据"><span>${esc(reading(field.item))}</span><span aria-hidden="true" class="plan-arrow">↗</span></button>`:'<p class="plan-missing">未提供，需核查</p>'}</div>`;
    const metadata=fields.filter(field=>['horizon','mode','expiry'].includes(field.key));
    const conditions=fields.filter(field=>['execution','invalidation','catalysts'].includes(field.key));
    const context=fields.filter(field=>['counter','benchmark'].includes(field.key));
    return `<section class="decision-plan ${preview?'':'report-plan'}" aria-label="配置与执行计划"><div class="plan-heading"><h3>配置与执行</h3><span>按原报告条件评估</span></div><div class="plan-metadata">${metadata.map(item).join('')}</div><div class="plan-conditions">${conditions.map(item).join('')}</div><details class="plan-context"><summary>反向证据与市场背景</summary>${context.map(item).join('')}</details></section>`;
  }
  function previewContent(selected){
    const report=state.preview?.id===selected.id?state.preview:null;
    if(!report)return state.previewError?`<div class="preview-empty"><p>${esc(state.previewError)}</p><button data-action="preview-retry">重新读取</button></div>`:'<div class="preview-loading" role="status"><span class="task-dot"></span>正在读取各维度重点…</div>';
    const data=report.preview;
    const source=(item,text,kind='')=>`<button class="preview-point ${kind}" data-preview-source="${esc(item.source)}" data-report-id="${esc(report.id)}" title="查看原文依据"><span>${esc(text||reading(item))}</span><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg></button>`;
    const empty={not_run:'未提供独立分析章节。',review:'该维度校验未通过，请先核查原文。',unstructured:'已有正文，暂未提取到可靠的独立摘要。',insufficient:'现有信息不足以形成该维度判断。',unassessed:'原报告未给出该维度评估。'};
    return `${report.warnings.length?`<details class="preview-data-note ${report.rating==='REVIEW'?'needs-review':''}"><summary>${report.rating==='REVIEW'?'报告需复核':'数据说明'} · ${report.warnings.length} 项</summary><ul>${report.warnings.map(text=>`<li>${esc(text)}</li>`).join('')}</ul></details>`:''}<section class="preview-verdict"><h3>总体结论</h3>${data.conclusion?source(data.conclusion):'<p class="muted">暂无独立结论，可打开完整研究核查。</p>'}</section>${decisionPlan(report,true)}<details class="report-glossary"><summary>术语说明</summary><p>EMA：指数移动平均线；SMA：简单移动平均线；MACD：平滑异同移动平均线；RSI：相对强弱指标；ATR：平均真实波幅；FCF：自由现金流；DPS：每股股息；ARPU：每用户平均收入；PE：市盈率；PB：市净率；ROE：净资产收益率。</p></details><div class="preview-angles">${data.dimensions.map(angle=>`<section class="preview-angle" data-angle="${esc(angle.key)}"><div class="angle-heading"><h3>${esc(angle.label)}</h3><span class="angle-status ${esc(angle.status)} ${esc(angle.kind)}">${angle.status==='ready'?(angle.kind==='scenario'?'条件情景':angle.kind==='risk'?'风险关注':'重点'):esc(angleStates[angle.status]||'待核查')}</span></div>${angle.items.length?angle.items.map((item,index)=>source(item,reading(item),index?'supporting-point':'')).join(''):`<p class="angle-empty">${esc(empty[angle.status]||'暂无可用摘要')}${angle.source?`<button class="link" data-preview-source="${esc(angle.source)}" data-report-id="${esc(report.id)}">查看原文 →</button>`:''}</p>`}</section>`).join('')}</div><section class="preview-followup"><h3>后续关注</h3>${data.follow_up?source(data.follow_up):'<p class="angle-empty">原报告未单独列出重新评估条件。</p>'}</section>`;
  }
  function fitPreview(){
    const pane=$('#today-detail');if(!pane)return;
    if(innerWidth<=700){pane.style.removeProperty('height');return;}
    const top=pane.getBoundingClientRect().top;const height=Math.max(240,innerHeight-Math.max(14,top)-20);
    if(Math.abs(pane.offsetHeight-height)>1)pane.style.height=height+'px';
  }
  async function ensurePreview(){
    if(state.page!=='today')return;
    const selected=state.pending.find(item=>item.id===state.selected)||state.pending[0];
    if(!selected||state.preview?.id===selected.id)return;
    if(state.preview&&state.preview.id!==selected.id)state.previewScroll=0;
    const token=++previewToken;
    try{const report=await api('get_preview',selected.id);const current=state.pending.find(item=>item.id===state.selected)||state.pending[0];if(token!==previewToken||state.page!=='today'||current?.id!==selected.id)return;
      state.preview=report;state.previewError='';const content=$('#preview-scroll');if(content){content.innerHTML=previewContent(selected);content.scrollTop=state.previewScroll||0;}fitPreview();
    }catch(error){if(token!==previewToken||state.page!=='today')return;state.previewError='速览暂未读取成功，请重试。';const content=$('#preview-scroll');if(content)content.innerHTML=previewContent(selected);}
  }
  async function inspect(id){++previewToken;state.selected=id;state.previewError='';state.previewScroll=0;render();}
  function revealSource(source){rememberDrafts();state.tab='evidence';state.section=source.replace(/-\d+$/,'');render();const el=document.getElementById(source);if(el){el.classList.add('highlight');el.scrollIntoView({block:'center',behavior:'smooth'});}}
  let previewFrame;
  window.addEventListener('resize',()=>{cancelAnimationFrame(previewFrame);previewFrame=requestAnimationFrame(fitPreview);});
  window.addEventListener('scroll',()=>{cancelAnimationFrame(previewFrame);previewFrame=requestAnimationFrame(fitPreview);},{passive:true});
  document.addEventListener('scroll',event=>{if(event.target.id==='preview-scroll')state.previewScroll=event.target.scrollTop;},true);
  function showHolding(ticker='') {
    const form=$('#holding-form');form.reset();const h=state.holdings.find(h=>h.ticker===ticker);
    form.elements.ticker.value=ticker;if(h){form.elements.quantity.value=h.quantity;form.elements.cost.value=h.cost;}
    $('#holding-stock-match').className='stock-match';$('#holding-stock-match').textContent='输入代码后自动识别名称与市场';clearValidation(form);$('#holding-dialog').showModal();form.elements.ticker.focus();if(ticker)resolveStockField(form.elements.ticker);
  }
  async function act(action) {
    if(action==='import') {
      if(state.busy)return;state.busy=true;toast('请选择包含报告的目录…');
      try {const result=await api('import_reports');if(!result.cancelled){await refreshData();render();toast(`已导入 ${result.added} 份，跳过重复 ${result.skipped} 份${result.errors.length?`，${result.errors.length} 份读取失败（请检查编码和文件格式）`:''}`);}}finally{state.busy=false;}
    } else if(action==='theme') {
      state.theme=state.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=state.theme;await api('set_theme',state.theme);$('#theme').setAttribute('aria-label',`切换${state.theme==='dark'?'浅色':'深色'}主题`);if(state.page==='settings')render();
    } else if(action==='refresh-reports') {await api('scan_reports');await refreshData();render();toast('报告已更新。');}
    else if(action==='reports-next'||action==='reports-prev'){state.offset=Math.max(0,state.offset+(action.endsWith('next')?100:-100));await loadReports();window.scrollTo(0,0);}
    else if(action==='filter-history'){state.historyTicker=$('#history-ticker').value.trim().toUpperCase();state.historyKind=$('#history-kind').value;state.historyOffset=0;await loadHistory();}
    else if(action==='history-next'||action==='history-prev'){state.historyOffset=Math.max(0,state.historyOffset+(action.endsWith('next')?100:-100));await loadHistory();window.scrollTo(0,0);}
    else if(action==='pending-next'||action==='pending-prev'){state.pendingOffset=Math.max(0,state.pendingOffset+(action.endsWith('next')?100:-100));state.pending=await api('pending_reports',state.pendingOffset);state.selected=null;render();window.scrollTo(0,0);}
    else if(action==='decisions-next'||action==='decisions-prev'){state.decisionOffset=Math.max(0,state.decisionOffset+(action.endsWith('next')?100:-100));state.stockRecords=await api('history',state.report.ticker,'decision',state.decisionOffset);render();}
    else if(action==='preview-retry'){state.previewError='';state.preview=null;render();}
    else if(action==='new-holding')showHolding();
    else if(action==='close-dialog')$('#holding-dialog').close();
    else if(action==='toggle-decision'){$('#decision-form').hidden=!$('#decision-form').hidden;if(!$('#decision-form').hidden)$('#decision-form textarea').focus();}
    else if(action==='export'){const result=await api('export_report',state.report.id);if(result.saved)toast('报告已导出。');}
    else if(action==='reanalyze'){state.drafts['analysis-ticker']=state.report.ticker;state.drafts['analysis-date']=localDate();go('analysis');}
    else if(action==='cancel'){state.task=await api('cancel_analysis');renderTask();toast('正在停止分析，已有历史报告仍可查看。');}
    else if(action==='retry-task') {const r=state.task.request;if(!r)throw new Error('请重新填写分析配置。');go('analysis');const f=$('#analysis-form');Object.entries(r).forEach(([k,v])=>{if(f.elements[k])f.elements[k].value=v;});window.StockSeekControls?.sync(f);resolveStockField(f.elements.ticker);toast('原配置已填入，点击开始分析重试。');}
    else if(action==='credential-check'){const provider=$('#credential-provider').value;const r=await api('credential_status',provider);$('#credential-status').textContent=r.required?(r.configured?'已配置，可用于新分析。':'尚未发现 API 凭据。'):'此服务商使用本地服务、订阅或企业身份认证。';}
    else if(action==='reader-find'){const query=$('#reader-search').value.trim();document.querySelectorAll('.source-block.highlight').forEach(el=>el.classList.remove('highlight'));const match=query&&[...document.querySelectorAll('.source-block')].find(el=>el.textContent.toLowerCase().includes(query.toLowerCase()));if(match){match.classList.add('highlight');match.scrollIntoView({block:'center',behavior:'smooth'});}else toast('当前章节没有找到匹配内容。');}
  }
  const stockLookups=new WeakMap();
  async function resolveStockField(input){
    const query=input.value.trim();const output=input.form?.querySelector('.stock-match');if(!output)return;
    output.className='stock-match';
    if(!query){output.textContent='输入代码后自动识别名称与市场';return;}
    const marker={};stockLookups.set(input,marker);output.textContent='正在识别…';
    try{const result=await api('resolve_stock',query);if(stockLookups.get(input)!==marker||input.value.trim()!==query||!input.isConnected)return;
      output.classList.toggle('resolved',result.resolved);
      output.innerHTML=result.resolved?`<span class="match-check">✓</span><strong>${esc(result.name)}</strong><span>${esc(result.code)} · ${esc(result.exchange)}</span>`:`<span>${esc(result.ticker)} · 暂无对应中文简称，将保留代码</span>`;
    }catch(error){if(stockLookups.get(input)===marker){output.textContent='名称暂未识别，可继续使用证券代码';}}
  }
  function clearFieldError(input){input.removeAttribute('aria-invalid');input.closest('label')?.querySelector('.field-error')?.remove();}
  function clearValidation(form){form.querySelectorAll('[aria-invalid]').forEach(clearFieldError);form.querySelectorAll('.field-error').forEach(el=>el.remove());}
  function showValidation(form){
    clearValidation(form);const invalid=[...form.elements].filter(input=>input.willValidate&&!input.validity.valid);
    invalid.forEach(input=>{input.setAttribute('aria-invalid','true');const message=document.createElement('span');message.className='field-error';message.setAttribute('role','alert');message.textContent=input.validity.valueMissing?'请填写此项':input.validity.rangeUnderflow?'请输入不小于 0 的数值':input.validity.stepMismatch?'请检查数值精度':'请输入有效的内容';input.closest('label')?.append(message);});invalid[0]?.focus();
  }
  document.addEventListener('click',async event=>{
    const button=event.target.closest('button');if(!button||button.disabled)return;
    try {
      if(button.id==='back'){++loadingReport;const previous=state.stack.pop();if(previous){rememberDrafts();Object.assign(state,previous);render();if(previous.decisionOpen&&$('#decision-form'))$('#decision-form').hidden=false;window.scrollTo(0,previous.scroll);}}
      else if(button.id==='theme')await act('theme');
      else if(button.dataset.page)go(button.dataset.page,!!button.closest('.sidebar'));
      else if(button.dataset.previewSource){const id=button.dataset.reportId;if(await openReport(id))revealSource(button.dataset.previewSource);}
      else if(button.dataset.report){const opened=await openReport(button.dataset.report);if(opened&&button.dataset.judgment){state.tab='judgment';render();$('#decision-form').hidden=false;}}
      else if(button.dataset.inspect)await inspect(button.dataset.inspect);
      else if(button.dataset.tab){rememberDrafts();state.tab=button.dataset.tab;render();}
      else if(button.dataset.section){state.section=button.dataset.section;render();}
      else if(button.dataset.source)revealSource(button.dataset.source);
      else if(button.dataset.holding)showHolding(button.dataset.holding);
      else if(button.dataset.action)await act(button.dataset.action);
    } catch(error){toast(error.message,true);}
  });
  document.addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target;if(!form.checkValidity()){showValidation(form);return;}clearValidation(form);
    const submit=form.querySelector('[type=submit]');if(submit)submit.disabled=true;
    try {
      const values=Object.fromEntries(new FormData(form));
      if(form.id==='analysis-form'){rememberDrafts();state.task=await api('start_analysis',values);renderTask();toast('分析已在后台启动，可继续浏览历史。');}
      else if(form.id==='settings-form'){state.settings=await api('save_settings',values);for(const key of ['analysis-quick','analysis-deep','analysis-url','analysis-provider'])delete state.drafts[key];toast('默认模型已保存。');}
      else if(form.id==='credential-form'){await api('save_credential',values.provider,values.key);form.elements.key.value='';toast('已保存到系统凭据库。');$('#credential-status').textContent='已保存，下次分析使用新凭据。';}
      else if(form.id==='decision-form'){await api('save_decision',state.report.id,values.action,values.reason);delete state.drafts[`decision-reason-${state.report.id}`];await refreshData();render();toast('个人判断已记录，实际持仓未自动变更。');}
      else if(form.id==='holding-form'){const result=await api('save_holding',values.ticker,Number(values.quantity),Number(values.cost),values.reason.trim()||'手动维护持仓');$('#holding-dialog').close();await refreshData();render();toast(result.saved?'实际持仓快照已保存。':'持仓没有变化，无需新增记录。');}
    }catch(error){toast(error.message,true);}finally{if(submit)submit.disabled=form.id==='analysis-form'&&taskActive();}
  });
  document.addEventListener('input',event=>{
    if(event.target.matches('input,textarea'))clearFieldError(event.target);
    if(event.target.name==='ticker'&&['holding-form','analysis-form'].includes(event.target.form?.id))resolveStockField(event.target);
    if(event.target.dataset.draft)state.drafts[event.target.dataset.draft]=event.target.value;
    if(event.target.id==='report-search'){state.query=event.target.value;state.offset=0;clearTimeout(searchTimer);const position=event.target.selectionStart;searchTimer=setTimeout(async()=>{try{await loadReports();const input=$('#report-search');if(input){input.focus();input.setSelectionRange(position,position);}}catch(error){toast(error.message,true);}},250);}
  });
  document.addEventListener('change',async event=>{try{if(event.target.id==='rating-filter'){state.rating=event.target.value;state.offset=0;await loadReports();}if(event.target.id==='credential-provider')$('#credential-status').textContent='点击检查当前服务商的凭据状态。';}catch(error){toast(error.message,true);}});
  document.addEventListener('keydown',event=>{
    if((event.metaKey||event.ctrlKey)&&event.key==='k'){event.preventDefault();if(state.page!=='research')go('research');$('#report-search')?.focus();}
    if((event.metaKey||event.ctrlKey)&&event.key==='n'){event.preventDefault();go('analysis');}
    if(event.target.matches('[role=tab]')&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const keys=Object.keys(tabs),index=keys.indexOf(state.tab);state.tab=event.key==='Home'?keys[0]:event.key==='End'?keys.at(-1):keys[(index+(event.key==='ArrowRight'?1:keys.length-1))%keys.length];render();$(`#tab-${state.tab}`)?.focus();}
    if(event.key==='Enter'&&event.target.id==='reader-search'){event.preventDefault();act('reader-find').catch(e=>toast(e.message,true));}
  });
  async function poll() {
    try {
      const [task, library]=await Promise.all([api('task_status'),api('library_status')]);
      state.task=task;state.library=library;renderTask();
      const statusKey=`${state.task.id}:${state.task.status}`;
      const taskChanged=statusKey!==seenTaskStatus;
      const libraryChanged=library.revision!==seenLibraryRevision;
      const firstCheck=state.page==='today'&&!state.allReports.length&&library.status==='watching'&&state.lastLibraryStatus!==library.status;
      state.lastLibraryStatus=library.status;
      seenTaskStatus=statusKey;seenLibraryRevision=library.revision;
      if(libraryChanged||(taskChanged&&state.task.status==='completed')){
        rememberDrafts();const scroll=window.scrollY;await refreshData();
        if(state.page==='today'||state.page==='research'){render();window.scrollTo(0,scroll);}
        else if(state.page==='history')await loadHistory();
        if(taskChanged&&state.task.status==='completed')toast('新的研究已完成，历史版本已保留。');
      }else if(firstCheck)render();
      if(taskChanged&&state.task.status==='failed')toast(state.task.phase,true);
    } catch(error){$('#footer-status').hidden=false;$('#footer-status').textContent='后台连接中断，请重新打开应用。';}
    pollTimer=setTimeout(poll,1500);
  }
  async function start() {
    bridge=window.pywebview?.api;if(!bridge)return;
    try {const data=await api('bootstrap');Object.assign(state,data,{allReports:data.reports,theme:data.theme||'dark'});document.documentElement.dataset.theme=state.theme;seenTaskStatus=`${state.task.id}:${state.task.status}`;seenLibraryRevision=state.library.revision;render();clearTimeout(pollTimer);poll();}catch(error){$('#connection-note').textContent=error.message;toast(error.message,true);}
  }
  new ResizeObserver(()=>document.documentElement.style.setProperty('--topbar-height',`${$('.topbar').offsetHeight}px`)).observe($('.topbar'));
  window.addEventListener('pywebviewready',start,{once:true});
  if(window.pywebview?.api)start();
  setTimeout(()=>{if(!bridge&&$('#connection-note'))$('#connection-note').textContent='此页面需要桌面应用连接。请运行 tradingagents-desktop，或 python -m tradingagents.desktop。';},2500);
})();
