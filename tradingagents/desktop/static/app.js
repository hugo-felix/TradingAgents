'use strict';
(() => {
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {reviews:'成交复盘',dividends:'高股息',today:'今日关注',research:'股票研究',holdings:'持仓管理',history:'历史记录',stock:'股票档案',analysis:'分析任务',settings:'应用设置'};
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
  function rememberDrafts() { captureStrategyDraft();document.querySelectorAll('[data-draft]').forEach(el=>state.drafts[el.dataset.draft]=el.value); }
  function restoreDrafts() { document.querySelectorAll('[data-draft]').forEach(el=>{if(Object.hasOwn(state.drafts,el.dataset.draft))el.value=state.drafts[el.dataset.draft];}); }
  function snapshot() {rememberDrafts();return {page:state.page,tab:state.tab,section:state.section,selected:state.selected,preview:state.preview,previewScroll:$('#preview-scroll')?.scrollTop||0,report:state.report,query:state.query,rating:state.rating,offset:state.offset,stockRecords:state.stockRecords,decisionOffset:state.decisionOffset,pendingOffset:state.pendingOffset,pending:state.pending,historyItems:state.historyItems,historyOffset:state.historyOffset,historyTicker:state.historyTicker,historyKind:state.historyKind,decisionOpen:!!document.querySelector('#decision-form:not([hidden])'),scroll:window.scrollY};}
  function go(page,root=false) {
    if (!Object.hasOwn(labels,page)) return;
    if (page===state.page && !root) return;
    ++loadingReport;rememberDrafts();if(root)state.stack=[];else state.stack.push(snapshot());
    state.page=page;render();window.scrollTo(0,0);
    if(page==='history')loadHistory();
    if(page==='dividends')loadDividends();
    if(page==='today')loadEntryPlan();
    if(page==='books')loadBooks();
    if(page==='strategies')loadStrategies();
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
    // Condition evidence is isolated from the definition editor and its drafts.
    $('#page-label').textContent=labels[state.page];$('#back').hidden=state.stack.length===0;
    document.querySelectorAll('.sidebar [data-page]').forEach(b=>{if(b.dataset.page===state.page||(state.page==='stock'&&b.dataset.page==='research'))b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    const views={reviews:()=>'<div data-trade-reviews></div>',strategies:strategies,books:books,dividends:dividends,today:today,research:research,holdings:holdings,history:history,stock:stock,analysis:analysis,settings:settings};
    window.StockSeekControls?.close();$('#content').innerHTML=views[state.page]();restoreDrafts();window.StockSeekControls?.enhance($('#content'));document.querySelectorAll('form').forEach(form=>form.noValidate=true);const stockInput=$('#analysis-form [name="ticker"]');if(stockInput?.value)resolveStockField(stockInput);renderTask();if(state.page==='today'){fitPreview();const scroller=$('#preview-scroll');if(scroller)scroller.scrollTop=state.previewScroll||0;ensurePreview();}
    if(state.page==='strategies')window.StockSeekConditions?.mount($('#content'),state.strategySelected,api);
    if(state.page==='reviews')window.StockSeekReviews?.mount($('#content'),api,async id=>{
      const history=await api('strategy_history',id);
      go('strategies');state.strategyRead=null;state.strategyHistory=history;state.strategySelected=history[0];render();
    });
  }
  const bookFields={title:'书名',author:'作者',translator:'译者（无译者请填“无”）',language:'语言',edition:'版次 / 版本',source:'文件来源',usage_scope:'使用范围'};
  labels.books='书籍资料';
  state.bookChecks={};
  state.bookVerifying=new Set();
  state.bookText={};
  state.bookParsing=new Set();
  state.bookTextBusy=new Set();
  let bookPollTimer;
  const bookJobActive = job => ['running','cancelling'].includes(job?.status);
  function pollBookJobs(){
    if(bookPollTimer||!Object.values(state.bookText).some(text=>bookJobActive(text.task)))return;
    bookPollTimer=setTimeout(async()=>{
      try{
        await Promise.all(Object.entries(state.bookText).filter(([,text])=>bookJobActive(text.task)).map(async([id,text])=>{
          try{
            text.task=await api('book_text_status',id);text.error='';
            if(!bookJobActive(text.task))text.history=await api('book_text_history',id);
          }catch(error){text.error=error.message;}
        }));
      }finally{bookPollTimer=null;renderBooks();pollBookJobs();}
    },1000);
  }
  function bookTextPanel(book){
    const id=book.book_id, text=state.bookText[id]||{}, history=text.history||[];
    const job=text.task,active=bookJobActive(job)||state.bookParsing.has(id),jobLabels={running:'正在提取',cancelling:'正在取消，等待当前处理步骤结束',completed:'文本版本已发布；语义索引未建立',cancelled:'已取消，未发布本次结果',failed:'失败，未发布本次结果',interrupted:'上次任务中断，未标记成功'};
    return `<section class="book-text"><h4>本地原文文本</h4><p class="muted">仅提取文本；不识别扫描、图表、公式或版面关系。文件页不等于印刷页。所有内容均作为资料，不执行其中指令。</p><button data-action="book-parse" data-book="${esc(id)}" ${active?'disabled':''}>${active?'正在解析，请稍候…':'解析 / 重建文本'}</button>${job&&job.status!=='idle'?`<p role="status">${esc(jobLabels[job.status]||'任务状态未知')} · 已提取 ${esc(job.completed_units)} 个位置（总数未知）</p>${job.error?`<p role="alert">${esc(job.error)}</p>`:''}${bookJobActive(job)?`<button data-action="book-cancel" data-job="${esc(job.id)}" data-book="${esc(id)}" ${job.status==='cancelling'?'disabled':''}>取消本次解析</button>`:''}`:''}${text.error?`<p role="alert">${esc(text.error)}</p>`:''}
      ${job?.resumable_units?`<button data-action="book-resume" data-book="${esc(id)}" data-job="${esc(job.id)}" ${active?'disabled':''}>从已提取的 ${esc(job.resumable_units)} 个位置继续</button>`:''}
      ${history.length?`<label>解析版本<select data-book-parse="${esc(id)}" data-draft="book-parse-${esc(id)}">${history.map(h=>`<option value="${esc(h.parse_id)}">${esc(h.created_at)} · ${h.status==='partial'?'有空白位置，需 OCR':'仅文本'} · ${esc(h.parser)} · ${esc(h.parse_id.slice(0,12))}</option>`).join('')}</select></label><label>原文关键词（多词均需匹配）<input data-book-query="${esc(id)}" data-draft="book-query-${esc(id)}" maxlength="200"></label><label>可选术语别名（每行：关键词 = 原文词或短语；同一关键词可重复多行）<textarea data-book-aliases="${esc(id)}" data-draft="book-aliases-${esc(id)}" maxlength="4000" placeholder="成交量 = volume"></textarea></label><p class="muted">别名仅用于本次查询，不自动翻译或保存为书籍知识。留空按原文关键词检索。</p><button data-action="book-search" data-book="${esc(id)}">检索原文</button>`:'<p class="muted">尚无成功发布的文本版本。</p>'}
      ${text.results?`<p role="status">${esc(text.results.limitations)} 共 ${text.results.total} 个位置，最多显示 50 个。</p>${text.results.matches.map(m=>`<details><summary>${m.kind==='pdf_file_page'?'PDF 文件页':'EPUB 阅读顺序章节'} ${m.ordinal}</summary><p>命中词：${esc(Object.entries(m.matched_terms||{}).map(([term,words])=>`${term} → ${words.join(' / ')}`).join('；'))}</p><p>${esc(m.excerpt)}</p><button data-action="book-citation" data-book="${esc(id)}" data-parse="${esc(m.parse_id)}" data-ordinal="${m.ordinal}">查看完整位置与出处</button></details>`).join('')}`:''}
      ${text.citation?`<details open class="book-citation"><summary>原文位置与出处</summary><p>${esc(text.citation.metadata.title)} · ${esc(text.citation.metadata.edition)} · ${text.citation.unit.kind==='pdf_file_page'?'PDF 文件页':'EPUB 阅读顺序章节'} ${text.citation.unit.ordinal} · 印刷页未知</p><p>快照状态：${esc(text.citation.original_status)} · 解析 ${esc(text.citation.parse_id)}</p><p>${esc(text.citation.unit.member||'')}</p><pre class="strategy-run-manifest">${esc(text.citation.unit.text||'此位置没有可提取文本；不能据此判断没有相关内容。')}</pre></details>`:''}</section>`;
  }
  function renderBooks(){if(state.page==='books'){rememberDrafts();render();}}
  async function loadBooks(){
    if(state.booksLoading)return;
    state.booksLoading=true;state.booksError='';renderBooks();
    try{state.books=await api('list_books');await Promise.all(state.books.map(async book=>{const text=state.bookText[book.book_id]??={};try{text.history=await api('book_text_history',book.book_id);text.task=await api('book_text_status',book.book_id);text.error='';}catch(error){text.error=error.message;}}));}catch(error){state.booksError=error.message;}
    finally{state.booksLoading=false;renderBooks();pollBookJobs();}
  }
  function books(){
    const checks={available:'指纹一致',missing:'原文快照缺失',changed:'原文快照已变更',unreadable:'原文快照不可读'};
    return `<div class="page-head"><div><h1>书籍资料</h1><p class="muted">登记原文和版本，供后续核对出处。文件仅保存在本机工作空间。</p></div></div>
      <section class="panel form-panel"><h2>登记 PDF / EPUB</h2><form id="book-form"><div class="form-grid">${Object.entries(bookFields).map(([key,label])=>`<label>${label}<input name="${key}" required data-draft="book-${key}"></label>`).join('')}</div><p class="muted">填写后选择文件。登记不会启动解析、OCR、索引或模型请求。</p><div class="actions"><button type="submit" class="primary" ${state.bookRegistering?'disabled':''}>${state.bookRegistering?'正在选择 / 登记…':'选择文件并登记'}</button></div></form></section>
      <section aria-label="已登记书籍"><div class="section-head"><h2>已登记书籍${state.books?` · ${state.books.length}`:''}</h2><button data-action="books-reload" ${state.booksLoading?'disabled':''}>刷新列表</button></div>
      ${state.booksError?`<p role="alert">读取失败：${esc(state.booksError)}。请刷新列表重试。</p>`:''}${state.booksLoading?'<p role="status">正在读取登记记录…</p>':''}
      ${!state.booksLoading&&!state.booksError&&state.books?.length===0?'<p class="empty">尚未登记书籍。请填写上方信息并选择文件。</p>':''}
      ${(state.books||[]).map(book=>{const check=state.bookChecks[book.book_id];return `<article class="panel book-record" data-book-id="${esc(book.book_id)}"><h3>${esc(book.metadata.title)}</h3><p>${esc(book.metadata.author)} · ${esc(book.metadata.edition)} · ${esc(book.metadata.language)}</p><p class="muted">${state.bookText[book.book_id]?.history?.length?"原文已登记 · 已有文本版本 · 语义索引未建立":"原文已登记 · 尚无已发布文本 · 语义索引未建立"}</p><details><summary>版本与来源详情</summary><dl>${Object.entries(bookFields).map(([key,label])=>`<dt>${label}</dt><dd>${esc(book.metadata[key])}</dd>`).join('')}<dt>原文件名 / 格式 / 大小</dt><dd>${esc(book.original_name)} · ${esc(book.format.toUpperCase())} · ${esc(book.size_bytes)} 字节</dd><dt>登记时间（非出版时间）</dt><dd>${esc(book.registered_at)}</dd><dt>书籍标识</dt><dd>${esc(book.book_id)}</dd><dt>SHA-256</dt><dd>${esc(book.sha256)}</dd></dl></details><div class="actions"><button data-action="book-verify" data-book="${esc(book.book_id)}" ${state.bookVerifying.has(book.book_id)?'disabled':''}>核验原文快照</button><span class="book-check" role="status">${state.bookVerifying.has(book.book_id)?'正在核验…':check?`${esc(checks[check.original_status]||'未知核验状态')} · ${esc(check.checked_at)}`:'尚未核验原文快照'}</span></div><p class="muted">核验仅检查本地快照指纹，不代表内容可解析；重复登记不会修复缺失或变更。</p>${bookTextPanel(book)}</article>`;}).join('')}</section>`;
  }
  const strategyFields={name:'策略名称',source:'来源 / 版本',market:'适用市场',universe:'股票池',timeframe:'观察周期',holding_period:'持有期限',environment:'适用环境',observation_time:'观察时点',confirmation_time:'确认时点',execution_time:'执行时点',execution_price:'成交价格规则'};
  const strategyRules={entry:'入场',filters:'过滤',exit:'退出',invalidation:'失效',position:'仓位',portfolio:'组合',exceptions:'例外'};
  const strategyKinds={author:'原作者规则',model_draft:'模型草案',personal:'个人规则',quantitative:'量化定义',market_adaptation:'市场适配'};
  const strategyRuleStates={pending:'待定义',manual:'人工检查',defined:'已定义'};
  labels.strategies='策略管理';
  function captureStrategyDraft(){
    const form=$('#strategy-form'),draft=state.strategyDraft;if(!form||!draft)return;
    for(const key of Object.keys(strategyFields)){const value=form.elements[key].value.trim();if(value)draft.definition[key]=value;else delete draft.definition[key];}
    form.querySelectorAll('[data-rule-row]').forEach(row=>{
      const rule=draft.definition.rules[row.dataset.group][Number(row.dataset.index)];
      row.querySelectorAll('[data-rule-field]').forEach(input=>rule[input.dataset.ruleField]=input.value);
    });
  }
  function renderStrategies(){if(state.page==='strategies'){rememberDrafts();render();}}
  async function loadStrategies(){
    if(state.strategiesLoading)return;
    state.strategiesLoading=true;state.strategiesError='';renderStrategies();
    try{state.strategies=await api('list_strategies');}catch(error){state.strategiesError=error.message;}
    finally{state.strategiesLoading=false;renderStrategies();}
  }
  const strategyStatus=doc=>doc.status==='confirmed'?'已确认':'草稿';
  function strategyOptions(options,value){return Object.entries(options).map(([key,label])=>`<option value="${key}" ${key===value?'selected':''}>${label}</option>`).join('');}
  function strategyEditor(){
    const draft=state.strategyDraft;if(!draft)return '';
    const definition=draft.definition;
    return `<section class="panel strategy-panel"><h2>${draft.strategy_id?`编辑 v${draft.version} 的新草稿`:'新建策略草稿'}</h2><p class="muted">只填名称即可保存范围草稿；新增的规则需写明内容及出处。未明确的条件保留“待定义”。离开页面会在本次窗口中保留草稿，关闭窗口前请保存。</p><form id="strategy-form"><div class="form-grid">${Object.entries(strategyFields).map(([key,label])=>`<label>${label}<input name="${key}" value="${esc(definition[key]||'')}" ${key==='name'?'required':''}></label>`).join('')}</div>
      ${Object.entries(strategyRules).map(([group,label])=>`<fieldset class="strategy-rule-group"><legend>${label}规则</legend>${(definition.rules[group]||[]).map((rule,index)=>`<div class="strategy-rule" data-rule-row data-group="${group}" data-index="${index}"><div class="form-grid"><label>来源类型<select data-rule-field="kind">${strategyOptions(strategyKinds,rule.kind)}</select></label><label>定义状态<select data-rule-field="state">${strategyOptions(strategyRuleStates,rule.state)}</select></label></div><label>规则内容<textarea data-rule-field="text" required>${esc(rule.text)}</textarea></label><label>出处 / 个人说明<textarea data-rule-field="citation" required>${esc(rule.citation)}</textarea></label><button type="button" data-action="strategy-remove-rule" data-group="${group}" data-index="${index}">移除此条${label}规则</button></div>`).join('')}<button type="button" data-action="strategy-add-rule" data-group="${group}">添加${label}规则</button></fieldset>`).join('')}
      <div class="actions"><button type="submit" class="primary" ${state.strategyWriting?'disabled':''}>保存新版本草稿</button><button type="button" data-action="strategy-discard" ${state.strategyWriting?'disabled':''}>放弃未保存编辑</button></div></form></section>`;
  }
  function strategyRunEvidence(doc){
    const key=`${doc.strategy_id}:${doc.version}`, result=state.strategyRunEvidence?.[key];
    return `<section aria-label="运行证据"><h3>v${doc.version} 的运行证据</h3><p class="muted">外部登记，未经核验，不代表已实现或通过验证。数据指纹及代码版本由登记者提供，当前不核验对应文件。</p><button data-action="strategy-runs">读取 / 刷新本版本证据</button>${result?.error?`<p role="alert">读取失败：${esc(result.error)}。请重试。</p>`:''}${result?.rows?result.rows.length?result.rows.map(row=>{const m=row.manifest;return `<article><h4>${esc(m.run_id)} · ${m.stage==='backtest'?'回测':'样本外'} · ${esc(({completed:'完成',failed:'失败',cancelled:'取消',inconclusive:'不确定'})[m.outcome])}</h4><p>${esc(m.summary)}</p><p class="muted">登记人：${esc(m.recorded_by)} · ${esc(row.recorded_at)} · 外部记录未核验</p><details><summary>数据、参数、代码及完整运行记录</summary><pre class="strategy-run-manifest">${esc(JSON.stringify(row,null,2))}</pre></details></article>`;}).join(''):'<p>本版本尚无登记记录，不代表没有风险或验证已通过。</p>':'<p class="muted">尚未读取本版本证据。</p>'}</section>`;
  }
  function strategyReview(){
    const doc=state.strategySelected;if(!doc)return '';
    const latest=state.strategyHistory?.[0];
    return `<section class="panel strategy-panel" aria-label="策略版本详情"><h2>${esc(doc.definition.name)} · v${doc.version} · ${strategyStatus(doc)}</h2><p class="muted">本地记录时间：${esc(doc.created_at)}。确认${doc.status==='confirmed'?'已完成':'未完成'} · 实现未完成 · 回测未评估 · 样本外未评估</p>${doc.confirmation?`<p>确认人：${esc(doc.confirmation.by)} · ${esc(doc.confirmation.at)} · 对应草稿 v${doc.confirmation.draft_version}</p>`:''}
      <div class="actions strategy-versions" aria-label="历史版本">${(state.strategyHistory||[]).map(item=>`<button data-action="strategy-version" data-version="${item.version}" ${item.version===doc.version?'disabled':''}>v${item.version} · ${strategyStatus(item)}</button>`).join('')}</div>
      <dl class="strategy-scope">${Object.entries(strategyFields).map(([key,label])=>`<dt>${label}</dt><dd>${esc(doc.definition[key]||'未填写')}</dd>`).join('')}</dl>
      ${strategyRunEvidence(doc)}
      ${Object.entries(strategyRules).map(([group,label])=>`<h3>${label}规则</h3>${doc.definition.rules?.[group]?.length?`<ul>${doc.definition.rules[group].map(rule=>`<li><p>${esc(rule.text)}</p><p class="muted">${esc(strategyKinds[rule.kind])} · ${esc(strategyRuleStates[rule.state])} · 出处：${esc(rule.citation)}</p></li>`).join('')}</ul>`:'<p class="muted">尚未填写</p>'}`).join('')}
      ${doc.version===latest?.version&&!state.strategyDraft?`<button data-action="strategy-edit">从此版本编辑</button>${doc.status==='draft'?`<form id="strategy-confirm-form"><p>请先逐项核对以上已保存版本。确认只代表接受该定义，不代表已实现或有效；缺少范围、任一类规则或仍有待定义规则时会拒绝确认。</p><label>确认人<input name="confirmed_by" required autocomplete="off"></label><label class="strategy-ack"><input type="checkbox" required>我已核对本版本的范围、交易时点、七类规则及出处</label><button type="submit" ${state.strategyWriting?'disabled':''}>确认已保存的 v${doc.version}</button></form>`:''}`:'<p class="muted">历史版本只读；有未保存编辑时请先保存或放弃编辑。</p>'}</section>`;
  }
  function strategies(){
    return `<div class="page-head"><div><h1>策略管理</h1><p class="muted">明确范围、规则和出处，再逐版核对。当前只管理定义，不生成信号或运行回测。</p></div><button data-action="strategy-new" ${state.strategyDraft||state.strategyWriting?'disabled':''}>新建策略</button></div>
      ${strategyEditor()}<section aria-label="策略列表"><div class="section-head"><h2>已保存策略</h2><button data-action="strategies-reload" ${state.strategiesLoading?'disabled':''}>刷新列表</button></div>${state.strategiesError?`<p role="alert">读取失败：${esc(state.strategiesError)}。请刷新重试。</p>`:''}${state.strategiesLoading?'<p role="status">正在读取策略…</p>':''}${!state.strategiesLoading&&!state.strategiesError&&state.strategies?.length===0?'<p class="empty">尚未保存策略。</p>':''}
      ${(state.strategies||[]).map(doc=>`<article class="panel strategy-list-item"><h3>${esc(doc.definition.name)}</h3><p>v${doc.version} · ${strategyStatus(doc)} · 实现未完成 · 回测未评估 · 样本外未评估</p><button data-action="strategy-open" data-strategy="${esc(doc.strategy_id)}">查看版本与规则</button></article>`).join('')}</section>${strategyReview()}`;
  }
  async function strategyAction(action,target){
    if(state.strategyWriting)return;
    state.strategyRead=null;
    if(action==='strategies-reload'){await loadStrategies();return;}
    captureStrategyDraft();
    if(action==='strategy-runs'){
      const doc=state.strategySelected, key=`${doc.strategy_id}:${doc.version}`;
      const marker={};state.strategyRead=marker;
      let result;
      try{result={rows:await api('list_strategy_runs',doc.strategy_id,doc.version)};}
      catch(error){result={error:error.message};}
      if(state.strategyRead!==marker)return;
      captureStrategyDraft();state.strategyRunEvidence??={};state.strategyRunEvidence[key]=result;
    }
    if(action==='strategy-new'){if(state.strategyDraft)return;state.strategyDraft={version:0,definition:{rules:{}}};}
    if(action==='strategy-discard')state.strategyDraft=null;
    if(action==='strategy-edit'){
      if(state.strategyDraft)return;
      const doc=state.strategySelected;state.strategyDraft={strategy_id:doc.strategy_id,version:doc.version,definition:JSON.parse(JSON.stringify(doc.definition))};
      state.strategyDraft.definition.rules??={};
    }
    if(action==='strategy-add-rule'){const rules=state.strategyDraft.definition.rules;rules[target.dataset.group]??=[];rules[target.dataset.group].push({kind:'personal',state:'pending',text:'',citation:''});}
    if(action==='strategy-remove-rule')state.strategyDraft.definition.rules[target.dataset.group].splice(Number(target.dataset.index),1);
    if(action==='strategy-open'){
      const marker={};state.strategyRead=marker;
      const history=await api('strategy_history',target.dataset.strategy);
      if(state.strategyRead!==marker)return;
      captureStrategyDraft();
      state.strategyHistory=history;state.strategySelected=history[0];
    }
    if(action==='strategy-version')state.strategySelected=state.strategyHistory.find(doc=>doc.version===Number(target.dataset.version));
    // Draft values have already been captured; do not read the old DOM after mutating rows.
    if(state.page==='strategies')render();
  }
  async function saveStrategyForm(form){
    if(state.strategyWriting)return;
    state.strategyRead=null;
    captureStrategyDraft();state.strategyWriting=true;
    try{
      let doc;
      if(form.id==='strategy-form'){
        const draft=state.strategyDraft;
        doc=await api('save_strategy',draft.definition,draft.strategy_id||null,draft.version);
        state.strategyDraft=null;
      }else{
        const selected=state.strategySelected;
        doc=await api('confirm_strategy',selected.strategy_id,selected.version,form.elements.confirmed_by.value);
      }
      // The write is successful even if a subsequent history/list read fails.
      state.strategySelected=doc;state.strategyHistory=[doc,...(state.strategyHistory||[]).filter(item=>item.strategy_id===doc.strategy_id)];
      await loadStrategies();toast(doc.status==='confirmed'?'已确认定义；实现与有效性仍待验证。':'已保存新版本草稿。');
    }finally{state.strategyWriting=false;renderStrategies();}
  }
  async function loadDividends() {
    const results=await Promise.allSettled([api('dividend_screen'),api('dividend_watchlist')]);
    if(results[0].status==='fulfilled'){state.dividendData=results[0].value;state.dividendError='';}
    else state.dividendError=results[0].reason.message;
    if(results[1].status==='fulfilled'){state.dividendWatches=results[1].value;state.dividendWatchError='';}
    else state.dividendWatchError=results[1].reason.message;
    if(state.page==='dividends')rememberDrafts();
    if(state.page==='dividends')render();
    await loadEntryPlan();
  }
  async function loadEntryPlan(preserveDrafts=true) {
    try {state.entryPlan=await api('entry_plan');state.entryError='';}
    catch(error){state.entryPlan=null;state.entryError=error.message;}
    if(state.page==='today'){if(preserveDrafts)rememberDrafts();render();}
  }
  function entryPreparation() {
    const plan=state.entryPlan,p=plan?.preferences;
    if(!plan)return `<section class="entry-preparation" aria-label="建仓准备"><h2>建仓准备</h2>${state.entryError?`<p role="alert">读取失败：${esc(state.entryError)}</p><button data-action="entry-reload">重新读取</button>`:'<p role="status">正在读取候选与个人计划…</p>'}</section>`;
    const money=n=>Number(n).toLocaleString('zh-CN',{maximumFractionDigits:2});
    const number=(n,suffix='')=>typeof n==='number'&&Number.isFinite(n)?`${esc(n.toFixed(2))}${suffix}`:'待核验';
    const candidates=plan?.candidates||[];
    const researched=candidates.filter(r=>state.allReports.some(item=>item.ticker.split('.')[0]===r.code)).length;
    const recorded=candidates.filter(r=>{const report=state.allReports.find(item=>item.ticker.split('.')[0]===r.code);return report&&state.records.some(item=>item.kind==='decision'&&item.report_id===report.id);}).length;
    const stale=plan?.as_of&&Math.floor((Date.parse(localDate())-Date.parse(plan.as_of))/86400000)>4;
    const environmentBusy=['running','cancelling'].includes(state.dividendJob?.status);
    const market=plan.market||{},portfolio=plan.portfolio||{},environmentLabels={normal:'未触及警戒线',caution:'触及风险警戒线',unknown:'待补数据'};
    return `<section class="entry-preparation" aria-label="建仓准备"><div class="section-head"><div><h2>选股工作台</h2><p class="muted">先看风险，再研究公司，最后记录自己的计划。</p></div><button class="quiet" data-page="dividends">全部高股息候选 →</button></div>
      <div class="entry-progress" aria-label="候选研究进度"><div><span>合格候选</span><strong>${candidates.length}<small>只</small></strong></div><div><span>待研究</span><strong>${candidates.length-researched}<small>只</small></strong></div><div><span>已有研究 · 待判断</span><strong>${researched-recorded}<small>只</small></strong></div><div><span>已记录个人判断</span><strong>${recorded}<small>只</small></strong></div></div>
      <div class="entry-environment"><section class="entry-market"><small>大盘 · 沪深300</small><h3>${esc(environmentLabels[market.status]||'待补数据')}</h3><p>${esc(market.reason||'请先刷新高股息清单')}</p><small>${esc(market.as_of||'日期待核验')} · 已完成交易日</small>${market.status!=='unknown'?`<details class="market-detail"><summary>查看风险指标</summary><dl class="market-metrics" aria-label="大盘风险指标"><div><dt>沪深300收盘 / 120日均线</dt><dd>${number(market.close)} / ${number(market.sma_120)}</dd><small>均线20日变化 ${number(market.sma_slope_20_pct,'%')}</small></div><div><dt>20日年化波动率</dt><dd>${number(market.volatility_20d_pct,'%')}</dd><small>警戒线 &gt; 30%</small></div><div><dt>60日最大回撤</dt><dd>${number(market.drawdown_60d_pct,'%')}</dd><small>警戒线 &gt; 15%</small></div></dl><p class="muted">${market.status==='caution'?'按当前预设，先复核风险再安排新增投入；仍可继续个股研究。':'可继续行业与个股研究，仍需核对估值、公告和实时价格。'}</p></details>`:''}<details class="market-basis"><summary>判断规则与数据来源</summary><p>${esc(plan.method||'判断规则待补齐')}</p><p>来源：${esc(market.endpoint||'待补齐')} · ${esc(market.symbol||'')}<br>采集：${esc(market.fetched_at||'时间待核验')}</p><p>回撤是区间内从高点到后续低点的最大跌幅，并非未来跌幅预测。规则未经回测，不是买卖信号。</p></details><div id="entry-environment-job" role="status">${environmentBusy?`<p>${esc(state.dividendJob.phase)}</p><button data-action="dividend-cancel">取消刷新</button>`:`<button data-action="entry-environment-refresh">${stale?'刷新全部筛选':'更新大盘与行业'}</button><button data-action="entry-environment-direct">直连重试</button>${state.dividendJob?.status==='failed'?`<p>${esc(state.dividendJob.phase)}</p>`:''}`}</div></section><section class="entry-portfolio"><small>我的持仓 · 行业分布</small><h3>${portfolio.status==='empty'?'未录入持仓':portfolio.status==='unknown'?'持仓覆盖不足':(portfolio.sectors||[]).map(s=>`${esc(s.sector)} ${number(s.weight_pct,'%')}`).join(' / ')}</h3><p>${portfolio.status==='empty'?'空仓可直接开始研究；已有股票请录入持仓。':portfolio.status==='unknown'?`无法完整计价：${esc((portfolio.missing_codes||[]).join('、'))}。请补充行情与行业资料。`:esc(portfolio.basis||'按已录入股票的快照市值计算')}</p><small>增持前检查同业集中度；完整保留所有候选</small><button class="quiet" data-page="holdings">管理持仓 →</button></section><section class="entry-personal"><small>我的计划 · 投入边界</small><h3>${p?`目标股息率 ${esc(p.target_yield)}%`:'尚未设置目标'}</h3><p>${p?`计划投入 ¥${money(p.capital)} · 持有 ${esc(p.horizon_years)} 年`:'设置后可计算目标股息对应价格。'}</p>
      <details class="entry-settings" ${p?'':'open'}><summary>${p?`我的计划 · 目标股息率 ${esc(p.target_yield)}% · 单只投入不超过 ¥${money(p.capital*p.position_pct/100)}`:'设置资金与股息目标'}</summary><form id="entry-preferences-form"><div class="entry-fields"><label>计划投入资金（元）<input name="capital" type="number" min="1" max="1000000000" step="0.01" required data-draft="entry-capital" value="${esc(p?.capital??'')}"></label><label>目标税前股息率（%）<input name="target_yield" type="number" min="4" max="30" step="0.01" required data-draft="entry-target" value="${esc(p?.target_yield??'')}"></label><label>单只投入上限（%）<input name="position_pct" type="number" min="1" max="100" step="0.01" required data-draft="entry-position" value="${esc(p?.position_pct??'')}"></label><label>预计持有年限<input name="horizon_years" type="number" min="1" max="50" step="0.5" required data-draft="entry-horizon" value="${esc(p?.horizon_years??'')}"></label></div><div class="actions"><button type="submit">保存个人计划</button><span class="muted">自主设定的上限，不代表适合你的仓位；资金可分批投入。</span></div></form></details></section></div>
      ${state.entryError?`<p role="alert">建仓数据读取失败：${esc(state.entryError)} <button data-action="entry-reload">重试</button></p>`:!plan?'<p class="muted" role="status">正在读取候选…</p>':!plan.as_of?'<div class="entry-empty"><h3>先获取一份高股息清单</h3><p>筛选完成后，这里会列出通过分红、财务和低波门槛的候选。</p><button data-page="dividends">前往筛选</button></div>':`<div class="section-head entry-caption"><h3>全部合格候选 · ${candidates.length} 只</h3><span class="muted">按年度股息率排序，非买入优先级 · 快照 ${esc(plan.as_of)}${stale?' · 数据较旧，请先刷新':''}</span></div>${candidates.length?`<div class="table-wrap entry-table"><table><thead><tr><th>股票 / 行业</th><th>年度股息率</th><th>价格 / 目标参考</th><th>1年风险</th><th>策略检查</th><th>下一步</th></tr></thead><tbody>${candidates.map(r=>{
        const report=state.allReports.find(item=>item.ticker.split('.')[0]===r.code),m=r.metrics;
        const decision=report&&state.records.find(item=>item.kind==='decision'&&item.report_id===report.id);
        const check=r.strategy||{},sector=check.sector||{};
        return `<tr data-entry-code="${esc(r.code)}"><td><strong>${esc(r.name)}</strong><small>${esc(r.code)} · ${esc(r.sector)}</small></td><td>${number(m.yield_pct,'%')}<small>${esc(m.dividend_year??'年份缺失')}年度 · 税前</small></td><td>${number(m.price,'元')}<small>${esc(m.quote_time||'行情时间待核验')}</small><div class="entry-reference"><small>目标参考</small>${r.reference_price==null?(p?'每股分红待核验':'先设置目标'):number(r.reference_price,'元')}<small>${p?'按已实施分红计算':'保存计划后计算'}</small></div></td><td>波动 ${number(m.volatility_1y_pct,'%')}<small>回撤 ${number(m.max_drawdown_1y_pct,'%')}</small></td><td class="entry-check"><strong>${esc(check.label||'待补数据')}</strong><small>行业：${esc(environmentLabels[sector.status]||'待补数据')}</small><details><summary>查看依据</summary><p>${esc((check.reasons||[]).join('；'))}</p><p>${esc(sector.reason||'行业行情待补齐')} · ${esc(sector.as_of||'日期待核验')}</p><p>60日相对沪深300 ${number(sector.relative_60d_pct,'个百分点')} · 来源 ${esc(sector.endpoint||'待补齐')} ${esc(sector.symbol||'')}</p><p>同业已录入股票市值占比：${check.portfolio_status==='empty'?'无已录入持仓':check.portfolio_status==='unknown'?'持仓数据不全':number(check.held_sector_pct??0,'%')}</p></details></td><td>${report?`<span>${report.rating==='REVIEW'?'研究需复核':decision?'已记录个人判断':'研究已完成 · 待你判断'}</span><small>${esc(report.date)} · 模型${esc(ratings[report.rating]||report.rating)}</small><button data-report="${esc(report.id)}">阅读结论与条件</button><button data-report="${esc(report.id)}" data-judgment="true">${decision?'查看 / 补充判断':'记录个人计划'}</button>${decision?`<small>本版记录：${esc(actions[decision.action]||decision.action)}</small><p class="entry-decision clamp">${esc(decision.text)}</p>`:''}`:`<span>尚未找到本地研究</span><button data-action="entry-research" data-code="${esc(r.code)}">填写研究配置</button>`}</td></tr>`;
      }).join('')}</tbody></table></div>`:'<p class="entry-empty">当前没有同时通过基础和精选门槛、年度股息率 >4% 的候选。可继续等待，或在高股息页面检查未通过的原因。</p>'}`}
      <details class="entry-method"><summary>策略说明与下单前检查</summary><p>${esc(plan.limitations||'环境数据待补齐。')}</p><p>${esc(plan.method||'')}</p><p>沪深300：120日均线 ${number(market.sma_120)} · 20日年化波动 ${number(market.volatility_20d_pct,'%')} · 60日回撤 ${number(market.drawdown_60d_pct,'%')} · 来源 ${esc(market.endpoint||'待补齐')} ${esc(market.symbol||'')}</p><ol><li>核对行业和公司的最新公告、分红可持续性、估值，以及研究中的反向证据和失效条件。</li><li>用券商实时价格复核目标股息率；开盘跳空、除权除息后须重新计算。</li><li>按自己的资金上限分批安排；先核对同业已投入金额，条件不足时继续等待。这里只统计已录入股票，不计算交易后的总资产仓位。</li></ol><p>参考价格 = 已实施年度每股税前分红 ÷ 目标股息率，向下取整到分。它不是估值结论，也不保证未来分红。持有年限仅记录个人意图，未用于收益预测。这里不监测实时价格，不判断条件已触发；模型“持有”不等于空仓可以买入。</p></details></section>`;
  }
  function dividendWatchlist() {
    const watches=state.dividendWatches||[],items=state.dividendData?.items||[];
    return `<details class="dividend-watches"><summary>我的自选 · ${watches.length} 只</summary><p class="muted">关注和备注保存在本机；退出筛选后仍保留。变化在成功刷新后更新。</p>${state.dividendWatchError?`<p role="alert">自选读取失败：${esc(state.dividendWatchError)}</p>`:''}${!watches.length?'<p>在下方候选列表点击“加入自选”。</p>':watches.map(w=>{
      const row=items.find(r=>r.code===w.code),change=state.dividendData?.changes?.items?.find(r=>r.code===w.code);
      return `<section class="dividend-watch" data-watch-code="${esc(w.code)}"><div class="section-head"><strong>${esc(w.name)} · ${esc(w.code)}</strong><button data-action="dividend-unwatch" data-code="${esc(w.code)}">移出自选</button></div><p>${row?`当前${row.core?.status==='pass'?'入选精选':row.core?.status==='fail'?'未入选精选':'待核验'} · 年度股息率 ${row.metrics.yield_pct==null?'缺失':Number(row.metrics.yield_pct).toFixed(2)+'%'}`:'未出现在当前清单；不能据此判断股息或财务恶化。'}</p>${change?`<p class="muted">本次变化：${esc(change.reasons.join('；'))}</p>`:''}<form class="dividend-note-form" data-code="${esc(w.code)}" data-name="${esc(w.name)}"><label>我的备注<textarea name="note" maxlength="2000" data-draft="dividend-note-${esc(w.code)}" rows="2" placeholder="关注理由、希望买入的价格或复核事项">${esc(w.note)}</textarea></label><button type="submit">保存备注</button></form></section>`;
    }).join('')}</details>`;
  }
  function dividendChanges(data) {
    const changes=data.changes;
    if(!changes?.previous_at)return '<p class="muted">尚无可比较的上次快照；下一次成功刷新后显示变化。</p>';
    const entries=changes.items||[],kinds={entered:'新入选',exited:'退出精选',updated:'指标变化'};
    return `<details class="dividend-changes"><summary>本次变化 · 新入选 ${entries.filter(r=>r.kind==='entered').length} · 退出精选 ${entries.filter(r=>r.kind==='exited').length} · 指标变化 ${entries.filter(r=>r.kind==='updated').length}</summary><p class="muted">对比上次成功快照 ${esc(new Date(changes.previous_at).toLocaleString('zh-CN',{hour12:false}))}。未重新采集的指标、口径不同及缺失数据不推断为恶化；不等于买卖建议。</p>${entries.length?`<ul>${entries.map(r=>`<li><strong>${esc(r.name)} · ${esc(r.code)} · ${esc(kinds[r.kind]||'变化')}</strong><p>${esc(r.reasons.join('；'))}</p></li>`).join('')}</ul>`:'<p>精选进出、4%门槛和同年度每股分红均无上述变化。</p>'}</details>`;
  }
  function dividendCoverage(data) {
    const roster=data.discovery?.roster;
    if(!roster)return '<p role="status">市场覆盖尚未对账，请刷新核验；未出现不代表不符合条件。</p>';
    const missing=roster.missing_quote_codes||[], extra=roster.unlisted_quote_codes||[];
    return `<details class="dividend-scope"><summary>市场覆盖${roster.status==='confirmed'?'已对账':'未确认'} · 行情 ${esc(roster.observed)} 只 · ${roster.status==='confirmed'?'交易所名单':'已取得的部分名单'} ${esc(roster.expected)} 只${missing.length?` · 行情待补 ${missing.length} 只`:''}</summary><p>${roster.status==='confirmed'?'已取得沪深北四个板块的股票名单并与行情逐一对账。':'部分交易所名单获取失败，目前不能确认全市场是否齐全。'} 行情遗漏的股票保留为候选继续核验。</p>${missing.length?`<p>行情缺失代码：${esc(missing.join('、'))}</p>`:''}${extra.length?`<p>行情中未匹配名单的代码：${esc(extra.join('、'))}</p>`:''}</details>`;
  }
  function dividendHealth(data) {
    const items=data.items||[],roster=data.discovery?.roster;
    const missingYield=items.filter(r=>r.metrics?.yield_pct==null).length;
    const pending=items.filter(r=>r.core?.status==='pending'||(!r.core&&r.status==='pending')).length;
    const calendar=data.trading_calendar;
    return `<section class="dividend-health" aria-label="数据完整性"><div><span>市场覆盖</span><strong>${roster?.status==='confirmed'?((roster.missing_quote_codes||[]).length?'名册已对账 · 行情待补':'名册已对账'):'未确认'}</strong><small>${roster?`行情 ${esc(roster.observed)} 只 · 行情待补 ${(roster.missing_quote_codes||[]).length} 只`:'旧快照缺少交易所名单对账'}</small></div><div><span>精选待核验</span><strong>${pending}<small> 只</small></strong><small>仅统计待核验状态；0只不代表数据齐全</small></div><div><span>年度股息率缺失</span><strong>${missingYield}<small> 只</small></strong><small>包含已因其他条件排除的股票；缺失不视为零</small></div><div><span>交易日历</span><strong>${calendar?.status==='available'?'已取得':'待补充'}</strong><small>${calendar?.status==='available'?`覆盖至 ${esc(calendar.last_date)}；范围外按工作日补更`:'暂按工作日补更，未识别节假日'}</small></div></section>`;
  }
  function dividendConcentration(rows) {
    if(!rows.length)return '';
    const counts=new Map();
    rows.forEach(r=>{const sector=r.sector||'行业未识别';counts.set(sector,(counts.get(sector)||0)+1);});
    const groups=[...counts].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0],'zh-CN'));
    const [sector,count]=groups[0],share=count/rows.length*100;
    return `<details class="dividend-concentration"><summary>当前列表行业分布 · ${esc(sector)} ${count}/${rows.length} 只（${share.toFixed(0)}%）${share>50?' · 行业集中':''}</summary><p>${groups.map(([name,n])=>`${esc(name)} ${n}只（${(n/rows.length*100).toFixed(0)}%）`).join(' · ')}</p><p>按当前筛选结果的股票数量统计，非持仓权重；同一行业可能受相同政策和周期影响。</p></details>`;
  }
  function dividendControls() {
    const job=state.dividendJob||{status:'idle'}, active=['running','cancelling'].includes(job.status);
    return `<div class="dividend-job" role="status" aria-live="polite">${job.status!=='idle'?`<span>${esc(job.phase)}${active?` · ${job.completed||0} / ${job.total||0} 只`:''}</span>`:''}<div class="actions">${active?`<button data-action="dividend-cancel" ${job.status==='cancelling'?'disabled':''}>${job.status==='cancelling'?'正在停止…':'取消刷新'}</button>`:`<button class="primary" data-action="dividend-refresh">${job.status==='failed'?'重试刷新':'刷新筛选'}</button><button data-action="dividend-reload">读取最新快照</button><details class="dividend-options"><summary>更新与连接</summary><button data-action="dividend-auto" aria-pressed="${job.auto_update!==false}">每日自动更新：${job.auto_update===false?'已关闭':'已开启'}</button><p>应用打开时检查快照；18:00 后按交易日历补更（日历未覆盖时按工作日），缺项或失败每小时重试，当日最多 3 次。首次采集需手动刷新。</p><p>默认沿用系统代理。连接失败时可尝试本次直连。</p><button data-action="dividend-direct">直连刷新</button></details>`}</div></div>`;
  }
  async function pollDividends() {
    const previous=state.dividendJob, job=await api('dividend_status');state.dividendJob=job;
    if(JSON.stringify(previous)!==JSON.stringify(job)){
      const controls=$('#dividend-job');if(controls){const focused=controls.contains(document.activeElement)?document.activeElement.dataset.action:null;controls.innerHTML=dividendControls();if(focused)controls.querySelector(`[data-action="${focused}"]:not(:disabled)`)?.focus();}
      if(job.status==='completed'&&(previous?.id!==job.id||previous?.status!=='completed')){await loadDividends();if(previous?.status==='running')toast('高股息筛选已更新。');}
      if(state.page==='today'){rememberDrafts();render();}
      if(job.status==='failed'&&previous?.status==='running')toast(job.phase,true);
    }
  }
  function dividends() {
    const data=state.dividendData, names={pass:'量化通过',pending:'待核验',fail:'未通过'};
    const filter=state.dividendFilter||'core';
    const sort=state.dividendSort||{key:'yield_pct',direction:'desc'};
    const sortNames={yield_pct:'年度已实施股息率',market_cap_yi:'总市值（亿元）',pe_annual:'静态PE',volatility_1y_pct:'1年波动率',volatility_3y_pct:'3年波动率',max_drawdown_1y_pct:'1年最大回撤',max_drawdown_3y_pct:'3年最大回撤',turnover_20d_yi:'20日日均成交额（亿元）'};
    const criteria=state.dividendCriteria||{}, period=criteria.period||'1';
    const sectors=[...new Set((data?.items||[]).map(r=>r.sector))].sort();
    const eligible=r=>{
      if(criteria.watched&&!(state.dividendWatches||[]).some(w=>w.code===r.code))return false;
      if(criteria.query&&!`${r.code} ${r.name}`.includes(criteria.query.trim()))return false;
      if(criteria.sector&&r.sector!==criteria.sector)return false;
      return [['minYield','yield_pct',true],['maxVol',`volatility_${period}y_pct`,false],['maxDrawdown',`max_drawdown_${period}y_pct`,false],['minTurnover','turnover_20d_yi',true]].every(([input,key,min])=>criteria[input]==null||criteria[input]===''||(numeric(r.metrics[key])&&(min?r.metrics[key]>=Number(criteria[input]):r.metrics[key]<=Number(criteria[input]))));
    };
    const numeric=v=>typeof v==='number'&&Number.isFinite(v);
    const highYield=r=>numeric(r.metrics.yield_pct)&&r.metrics.yield_pct>4;
    const viewNames={core:'红利低波精选',core_pending:'精选待核验',yield:'股息率 >4%',all:'全部核验记录',...names};
    const matches=(r,k)=>k==='all'||(k==='core'?r.core?.status==='pass':k==='core_pending'?r.core?.status==='pending':k==='yield'?highYield(r):r.status===k);
    const inView=r=>matches(r,filter);
    const rows=(data?.items||[]).filter(r=>inView(r)&&eligible(r)).sort((a,b)=>{
      const x=a.metrics[sort.key],y=b.metrics[sort.key];
      if(!numeric(x)||!numeric(y))return numeric(x)?-1:numeric(y)?1:String(a.code).localeCompare(String(b.code));
      return (sort.direction==='desc'?y-x:x-y)||String(a.code).localeCompare(String(b.code));
    });
    const columnNames={yield_pct:'年度股息率',market_cap_yi:'总市值 · 亿',pe_annual:'静态 PE',volatility_1y_pct:'波动率 · 1年',volatility_3y_pct:'波动率 · 3年',max_drawdown_1y_pct:'回撤 · 1年',max_drawdown_3y_pct:'回撤 · 3年',turnover_20d_yi:'日均成交 · 亿'};
    const viewNotes={core:'分红、财务与低波门槛均通过',yield:'仅通过股息门槛，仍需核验财务与风险',core_pending:'证据尚未齐备，暂不纳入精选'};
    const sortHeader=key=>`<th aria-sort="${sort.key===key?(sort.direction==='desc'?'descending':'ascending'):'none'}"><button class="dividend-sort" data-action="dividend-sort" data-sort="${key}">${columnNames[key]} ${sort.key===key?(sort.direction==='desc'?'↓':'↑'):'↕'}</button></th>`;
    const fmt=(v,suffix='')=>v==null?'缺失':`${Number(v).toFixed(2)}${suffix}`;
    const stale=data && (Date.now()-Date.parse(data.generated_at)>4*86400000);
    const advancedActive=period==='3'||['minYield','maxVol','maxDrawdown','minTurnover'].some(k=>criteria[k]!=null&&criteria[k]!=='');
    const reason=r=>{
      const checks=[...(r.core?.checks||[]),...(r.checks||[])].filter(c=>c.required!==false);
      const issues=checks.filter(c=>c.status==='fail');
      const pending=checks.filter(c=>c.status==='pending');
      const labels=[...new Set((issues.length?issues:pending).map(c=>c.label==='A股上市满3年'?'上市历史不足':c.label))];
      return labels.length?`${issues.length?'未通过':'待补充'}：${labels.slice(0,2).join('、')}${labels.length>2?` 等${labels.length}项`:''}`:r.core?.status==='pass'?'分红、财务与低波门槛通过':'缺少精选核验依据';
    };
    return `<div class="page-head dividend-page-head"><div><h1>高股息看板</h1><p>以已实施分红为准，比较收益与风险</p></div><div id="dividend-job">${dividendControls()}</div></div>

      ${state.dividendError?`<p role="alert">${esc(state.dividendError)}</p>`:''}
      ${data?.official_evidence_error?`<p role="alert">${esc(data.official_evidence_error)}</p>`:''}
      ${data&&data.schema_version!==2?'<p role="alert">尚未完成全市场采集：当前为旧版固定候选池，未出现不代表不符合条件。请刷新核验。</p>':''}
      ${data?.schema_version===2?dividendCoverage(data):''}
      ${!data?dividendWatchlist()+'<div class="empty"><h2>尚无筛选快照</h2><p>点击“刷新筛选”，获取行情、分红记录与公开财报。首次运行可能需要数分钟，可切换页面或取消。</p></div>':`
      <div class="dividend-freshness"><span class="dividend-snapshot-label">筛选快照</span><span>更新于 <time datetime="${esc(data.generated_at)}">${esc(new Date(data.generated_at).toLocaleString('zh-CN',{hour12:false}))}</time></span><span>数据截至 ${esc(data.as_of)}</span></div>
      ${dividendHealth(data)}
      <aside class="dividend-risk-scope" aria-label="核验范围"><strong>精选仅通过量化门槛</strong><span>审计意见、最新季报恶化、特别分红与未来分红尚需单独复核。</span></aside>
      <p class="dividend-yield-definition">筛选与排序使用年度已实施税前股息率，须严格大于 4%。TTM 仅供参考；分红年度、每股金额和行情时间见“核验依据”。</p>
      ${dividendChanges(data)}

      ${stale?'<p role="alert">快照已超过4天，请重新抓取后再评估。</p>':''}
      ${data.items.some(r=>r.official_evidence?.length)?'<p class="muted">部分缺项已按官方公告补充；点击核验依据可查看主体、期间、来源和页码。公告补充不改变原始采集时间，也不代表全部条件通过。</p>':''}

      <div class="dividend-overview" role="group" aria-label="按股息门槛或核验状态筛选">${['core','yield','core_pending'].map(k=>`<button class="dividend-stat" data-action="dividend-filter" data-filter="${k}" aria-pressed="${filter===k}"><span class="dividend-stat-label">${viewNames[k]}</span><strong>${data.items.filter(r=>matches(r,k)).length}<small>只</small></strong><span class="dividend-stat-note">${viewNotes[k]}</span></button>`).join('')}</div>

      <section class="dividend-workspace" aria-label="高股息候选列表"><div class="dividend-results-head"><div><h2>${viewNames[filter]} <span>${rows.length}<small> 只</small></span></h2><p>${viewNotes[filter]||'核验记录含未通过或数据缺失的股票，请留意状态'}</p></div><span class="dividend-order">${sortNames[sort.key]}${sort.direction==='desc'?' ↓':' ↑'} · 缺失值置后</span></div>
      ${dividendConcentration(rows)}
      <form id="dividend-filters" class="dividend-filters">
        <label>股票搜索<input name="query" value="${esc(criteria.query||'')}" placeholder="名称或代码"></label>
        <label>行业<select name="sector"><option value="">全部行业</option>${sectors.map(v=>`<option ${criteria.sector===v?'selected':''}>${esc(v)}</option>`).join('')}</select></label>
        <label class="dividend-watch-filter"><input type="checkbox" name="watched" ${criteria.watched?'checked':''}>只看自选</label>
        <div class="actions"><button type="submit" class="primary">应用筛选</button><button type="button" data-action="dividend-reset">重置条件</button></div>
        <details class="dividend-advanced" ${advancedActive?'open':''}><summary>更多筛选${advancedActive?' · 已启用':''}</summary><div class="dividend-advanced-grid">
        <label>风险窗口<select name="period"><option value="1" ${period==='1'?'selected':''}>近1年</option><option value="3" ${period==='3'?'selected':''}>近3年</option></select></label>
        ${[['minYield','最低股息率（%）'],['maxVol','最高年化波动率（%）'],['maxDrawdown','最高最大回撤（%）'],['minTurnover','最低日均成交额（亿元）']].map(([key,label])=>`<label>${label}<input type="number" min="0" step="any" name="${key}" value="${esc(criteria[key]??'')}" placeholder="不限"></label>`).join('')}
        </div><p class="muted">启用数值门槛后，缺失该指标的股票不进入结果。</p></details>
      </form>

      ${!rows.length?`<div class="empty"><h2>当前分类没有候选</h2><p>可切换其他核验状态，或刷新筛选获取最新结果。</p><button data-action="dividend-filter" data-filter="yield">查看股息率 >4% 候选</button><button data-action="dividend-filter" data-filter="core_pending">查看精选待核验</button></div>`:''}<div class="table-wrap dividend-table-scroll" tabindex="0" role="region" aria-label="股票指标对比，可横向滚动" ${!rows.length?'hidden':''}><table class="research-table dividend-table"><caption class="sr-only">${viewNames[filter]}，共 ${rows.length} 只；点击列标题排序</caption><thead><tr><th>股票</th>${sortHeader('yield_pct')}${sortHeader('market_cap_yi')}${sortHeader('pe_annual')}${sortHeader(`volatility_${period}y_pct`)}${sortHeader(`max_drawdown_${period}y_pct`)}${sortHeader('turnover_20d_yi')}<th>精选结论 / 原因</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${esc(r.name)}</strong><small>${esc(r.code)} · ${esc(r.sector)}</small><button data-action="dividend-watch" data-code="${esc(r.code)}" data-name="${esc(r.name)}" aria-pressed="${(state.dividendWatches||[]).some(w=>w.code===r.code)}">${(state.dividendWatches||[]).some(w=>w.code===r.code)?"已关注":"加入自选"}</button></td><td>${fmt(r.metrics.yield_pct,'%')}<small>${r.metrics.dividend_year==null?'年度缺失':esc(r.metrics.dividend_year)+'年'} · 税前</small><small>TTM ${fmt(r.metrics.yield_ttm_pct,'%')}</small></td><td>${fmt(r.metrics.market_cap_yi)}</td><td>${fmt(r.metrics.pe_annual)}</td><td>${fmt(r.metrics[`volatility_${period}y_pct`],'%')}</td><td>${fmt(r.metrics[`max_drawdown_${period}y_pct`],'%')}</td><td>${fmt(r.metrics.turnover_20d_yi)}</td><td><span class="dividend-status ${esc(r.core?.status||'pending')}">${r.core?.status==='pass'?'入选':r.core?.status==='fail'?'未入选':'待核验'}</span><small class="dividend-reason">${esc(reason(r))}</small><button data-action="dividend-evidence" aria-haspopup="dialog">核验依据</button><dialog class="dividend-evidence" aria-labelledby="dividend-title-${esc(r.code)}"><header><h2 id="dividend-title-${esc(r.code)}">${esc(r.name)} · ${esc(r.code)}</h2><button data-action="dividend-evidence-close" aria-label="关闭核验依据">关闭</button></header><p>基础核验：${esc(names[r.status]||'未知')} · 精选：${r.core?.status==='pass'?'入选':r.core?.status==='fail'?'未入选':'待核验'}</p><h4>精选依据</h4><p>年度每股派息率：${fmt(r.metrics.payout_ratio_pct,'%')} · 基本EPS：${fmt(r.metrics.basic_eps_annual,'元')}</p><ul>${(r.core?.checks||[]).map(c=>`<li>${esc(names[c.status])}：${esc(c.label)} ${esc(c.detail)}</li>`).join('')}</ul><h4>基础核验依据</h4><ul>${r.checks.map(c=>`<li>${c.required===false?'参考项（不作门槛） · ':''}${esc(names[c.status])}：${esc(c.label)} ${esc(c.detail)}</li>`).join('')}</ul>${(r.risk_notes||[]).map(n=>`<p>${esc(n)}</p>`).join('')}<p>低波行情截至：${esc(r.metrics.risk_as_of||'缺失')}</p>${r.provenance?.dividends?.endpoint==='stock_dividend_cninfo'?'<p>分红数据已由备用来源巨潮资讯获取；其他缺项仍按核验结果显示。</p>':''}${r.errors?.length?`<details><summary>采集来源提示与历史记录（${r.errors.length}）</summary>${r.errors.map(e=>`<p>${esc(e)}</p>`).join('')}</details>`:''}<p>年度口径：${esc(r.metrics.dividend_year??'缺失')}年，每股税前现金分红 ${fmt(r.metrics.dividend_annual,'元')} ÷ 行情价格 ${fmt(r.metrics.price,'元')}。TTM 每股分红 ${fmt(r.metrics.dividend_ttm,'元')}，仅供参考。</p><p>行情时间：${esc(r.metrics.quote_time||'缺失')}</p><p>来源：${Object.values(r.sources||{}).map(url=>esc(url)).join('<br>')}</p></dialog></td></tr>`).join('')}</tbody></table></div></section><div class="dividend-reference"><h2>跟踪与核验</h2>${dividendWatchlist()}<details class="dividend-scope"><summary>筛选规则与数据范围</summary><p>${esc(data.coverage)}</p><p>${esc(data.rules)}</p><p>${esc(data.core_rules||'旧快照缺少精选依据，请刷新筛选。')}</p><p>${esc(data.limitations)}</p><p>不限制实际控制人性质及行业排名。${data.schema_version===2?`扫描全A股，核验市值≥500亿元及市值缺失的候选；低波指标齐备 ${data.discovery.risk_complete} / ${data.items.length} 只。${data.discovery.provider_error_count} 只附有采集来源提示（含已恢复的接口切换）。${data.discovery.yield_excluded||0} 只年度股息率未超过4%，未继续采集财务与低波数据。`:'当前为旧版候选快照。'}</p><p>仅按快照日期核验；审计意见、最新季报恶化、特别分红及未来分红仍需复核。年度每股派息率采用基本EPS估算，现金流覆盖为累计净利润口径。筛选通过不代表低风险。</p></details><details class="dividend-records" ${['pass','pending','fail','all'].includes(filter)?'open':''}><summary>其他核验记录${['pass','pending','fail','all'].includes(filter)?` · ${viewNames[filter]}`:''}</summary><div class="actions">${['pass','pending','fail','all'].map(k=>`<button class="dividend-stat" data-action="dividend-filter" data-filter="${k}" aria-pressed="${filter===k}">${viewNames[k]} · ${data.items.filter(r=>matches(r,k)).length}</button>`).join('')}</div></details><details class="dividend-method"><summary>指标口径与快照说明</summary><p>年度已实施股息率 = 对应年度已实施的每股税前现金分红 ÷ 行情价格；TTM仅供参考。金额、价格与行情时间见核验依据。</p><p>波动率为日收益标准差×√252，回撤显示跌幅绝对值；采用前复权收盘价，排除当日未完成行情。刷新失败或取消时保留上次成功快照。</p></details></div>`}`;
  }
  function today() {
    const updates=state.pending;
    const selected=updates.find(r=>r.id===state.selected)||updates[0];
    const date=new Intl.DateTimeFormat('zh-CN',{month:'long',day:'numeric',weekday:'long'}).format(new Date());
    return `<div class="page-head today-head"><div><h1>今日关注</h1><p>${esc(date)}</p></div><div class="page-meta" data-monitoring-status><span class="status-dot"></span><span>本地报告 · 自动更新</span><details class="scope-popover"><summary aria-label="查看数据覆盖范围">数据范围</summary><p>自动读取本机研究报告。行情与公告尚未持续监测。</p></details></div></div>
      ${state.holdings.some(h=>h.quantity>0)?`<details class="entry-held"><summary>建仓准备 · 大盘、行业与增持检查</summary>${entryPreparation()}</details>`:entryPreparation()}
      ${state.allReports.length||updates.length?`<div class="section-head workspace-heading"><h2>研究更新 <span class="count">${updates.length===100?'100+':updates.length}</span></h2><span class="muted">按分析日期排序</span></div><div class="split"><div class="queue">${updates.length?updates.map(r=>`<button class="queue-row" data-inspect="${esc(r.id)}" aria-pressed="${r.id===selected?.id}"><div class="queue-identity"><div><strong>${nameOf(r)}</strong><span class="stock-code">${esc(r.ticker)}</span></div>${badge(r.rating)}</div><p class="preview-text">${esc(r.display_conclusion||r.conclusion||'暂无独立结论，打开研究查看依据。')}</p><div class="queue-foot"><time>${esc(r.date)}</time><span>${r.rating==='REVIEW'?'需复核':r.has_previous?'更新研究':'首次研究'}</span></div></button>`).join(''):`<div class="empty"><h3>研究均已留下判断</h3><p class="muted">可在股票研究中回看。</p></div>`}</div>
      ${selected?`<section id="today-detail" class="inspector research-preview" aria-label="研究速览"><header class="preview-header"><div class="preview-heading"><span>研究速览</span><time>分析日期 ${esc(selected.date)}</time></div><div class="preview-stock"><h2>${nameOf(selected)} <span class="muted">${esc(selected.ticker)}</span></h2><div class="preview-rating"><span>模型观点</span>${badge(selected.rating)}</div></div></header><div id="preview-scroll" class="preview-scroll" tabindex="0" aria-label="速览内容，可滚动">${previewContent(selected)}</div><footer class="preview-actions"><div class="actions"><button class="primary" data-report="${esc(selected.id)}">打开研究</button><button data-report="${esc(selected.id)}" data-judgment="true">记录判断</button></div><span>原文摘录 · 点击重点查看依据</span></footer></section>`:''}</div>`:
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
      <aside class="summary-aside"><section class="side-note research-next"><h2>看完之后做什么？</h2><p>核对执行条件、反向证据和失效条件，再记录自己的选择：继续等待、补充研究，或制定个人计划。</p><button data-report="${esc(r.id)}" data-judgment="true">记录个人计划 →</button><p class="muted">研究完成不代表买入条件已满足；模型“持有”也不等于空仓可以买入。</p></section><section class="side-note"><h2>反向证据与风险</h2>${excerpt(s.counter)}</section><section class="side-note"><h2>重新评估条件</h2>${excerpt(s.conditions)}</section><div class="summary-context"><span>当前版本</span><strong>${esc(r.id.slice(0,8))}</strong><p>${esc(r.date)} 的研究快照</p></div></aside></div>`;
  }
  function evidence(r) {
    const section=r.sections.find(s=>s.id===state.section)||r.sections.find(s=>s.id==='decision')||r.sections[0];
    return `
      <details><summary>数据来源与校验记录（${r.coverage.length} 项来源）</summary>${r.coverage.length?`<div class="table-wrap"><table><thead><tr><th>引用</th><th>工具</th><th>可用状态</th></tr></thead><tbody>${r.coverage.map(c=>`<tr><td>${esc(c.reference)}</td><td>${esc(c.tool)}</td><td>${esc(sourceStates[c.status]||'未知')}${c.html?`<details><summary>原始返回内容</summary><div class="source-block">${c.html}</div></details>`:''}</td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">此报告没有可用的来源清单。保留的段落只作为模型原文，不冒充独立事实验证。</p>'}</details>
      <div class="reader-layout"><nav class="reader-nav" aria-label="依据主题">${r.sections.filter(s=>!s.debate).map(s=>`<button data-section="${esc(s.id)}" aria-pressed="${s.id===section?.id}">${esc(s.title)}</button>`).join('')}</nav><div class="reader panel"><div class="section-head"><h2>${esc(section?.title||'原文依据')}</h2><span class="muted">${esc(r.date)}</span></div>${(section?.blocks||[]).map(block=>`<div class="source-block" id="${esc(block.id)}">${block.display_html||block.html}</div>`).join('')}</div></div>`;
  }
  function judgment(r) {
    const records=state.stockRecords;
    return `<div class="summary-layout"><div><div class="panel"><div class="section-head"><h2>我的研究判断</h2><button data-action="toggle-decision">记录新判断</button></div><p class="muted">模型观点：${esc(ratings[r.model_rating]||r.model_rating)} · ${esc(r.date)}。个人计划与实际持仓分别维护。</p><form id="decision-form" hidden><label>处理方式<select name="action" data-draft="decision-action-${esc(r.id)}">${Object.entries(actions).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label><label>判断依据<textarea name="reason" data-draft="decision-reason-${esc(r.id)}" required maxlength="10000" placeholder="例如：等待价格达到什么条件；最多投入多少；哪些风险出现就放弃；何时重新检查。"></textarea></label><div class="actions"><button type="submit" class="primary">保存个人判断</button><button type="button" data-action="toggle-decision">暂不记录</button></div><p class="muted">“安排重新研究”会保留你的研究意图，需要由你启动实际分析。</p></form></div><div class="timeline">${records.length?records.map(recordHTML).join(''):'<p class="muted">尚未记录个人判断。没有新的想法时可以跳过。</p>'}</div><div class="pagination"><button data-action="decisions-prev" ${state.decisionOffset===0?'disabled':''}>上一页判断</button><span>第 ${state.decisionOffset/100+1} 页</span><button data-action="decisions-next" ${records.length<100?'disabled':''}>下一页判断</button></div></div><div class="panel side-note"><h2>这次判断对应的研究</h2><div class="row"><span>分析日期</span><span>${esc(r.date)}</span></div><div class="row"><span>模型评级</span><span>${esc(ratings[r.rating])}</span></div><div class="row"><span>报告版本</span><span>${esc(r.id.slice(0,8))}</span></div><p>记录后返回今日关注，按自己写下的条件继续观察。未满足时继续等待；页面不监测实时价格。</p><div class="actions"><button data-page="today">返回今日关注</button><button data-page="holdings">已成交？录入持仓</button></div><p class="muted">只有实际成交后才录入数量与成本。保存判断不会自动下单。</p></div></div>`;
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
    return `<div class="page-head"><div><h1>分析任务</h1><p>运行中的研究与任务记录</p></div></div><div id="task-detail" class="panel form-panel">${taskDetail()}</div><div class="panel form-panel"><form id="analysis-form"><div class="form-grid"><label class="stock-field">股票代码<input name="ticker" data-draft="analysis-ticker" placeholder="601658 / 600941 / AAPL" required maxlength="40" autocomplete="off" aria-describedby="analysis-stock-match"><div id="analysis-stock-match" class="stock-match" role="status" aria-live="polite">输入代码后自动识别名称与市场</div></label><label>信息截止日期<input name="date" data-draft="analysis-date" type="date" value="${localDate()}" max="${localDate()}" required aria-describedby="analysis-date-help"><span id="analysis-date-help" class="muted">研究当前情况请选择今天（含休市日）。选择过去日期时，无法确认当时版本的财报、分红与讨论会留空，行情取截止日内最近交易日。</span></label></div>${modelFields('analysis')}<p class="muted">使用配置服务商的 API，会产生相应费用。凭据从环境变量或系统凭据库读取；新结果单独保存。</p><div class="actions"><button class="primary" type="submit" ${taskActive()?'disabled':''}>开始研究</button><button type="button" data-page="settings">配置凭据</button></div></form></div>`;
  }
  function taskDetail() {
    const t=state.task;
    if(t.status==='idle')return '<h3>尚无分析任务</h3><p class="muted">运行后会显示真实阶段与耗时，不估算虚假完成百分比。</p>';
    return `<span class="eyebrow">分析进度 · ${esc(t.ticker)} · ${esc(t.date)}</span><div class="task-detail"><strong>${esc(t.phase||t.status)}</strong><span class="muted">${elapsed(t)}${t.error_kind?` · ${esc(t.error_kind)}`:''}</span></div>${taskProgress(t)}<div class="actions">${taskActive()?`<button data-action="cancel" ${t.status==='cancelling'?'disabled':''}>取消分析</button>`:''}${t.status==='completed'?`<button class="primary" data-report="${esc(t.report_id)}">查看新研究</button>`:''}${['failed','cancelled','interrupted'].includes(t.status)?'<button data-action="retry-task">按原配置重试</button>':''}</div>`;
  }
  function taskProgress(t) {
    const steps=t.progress||[];if(!steps.length)return '';
    const done=steps.filter(s=>s.status==='completed').length;
    const labels={completed:'已完成',running:t.status==='running'?'进行中':t.status==='cancelling'?'正在停止':'已停止',pending:taskActive()?'等待中':'未执行'};
    return `<div class="task-progress"><div class="progress-caption"><span>已完成 ${done} / ${steps.length} 项</span><span class="muted">${t.status==='running'&&t.phase_started_epoch?`当前阶段${elapsed({...t,started_epoch:t.phase_started_epoch})}`:'按实际完成项更新'}</span></div><div class="progress-segments" aria-label="已完成 ${done} / ${steps.length} 项">${steps.map(s=>`<span class="${s.status==='completed'?'completed':s.status==='running'&&t.status==='running'?'running':''}"></span>`).join('')}</div><ol class="progress-steps">${steps.map(s=>`<li class="${s.status==='completed'?'completed':s.status==='running'&&t.status==='running'?'running':''}"><span class="step-marker" aria-hidden="true">${s.status==='completed'?'✓':s.status==='running'&&t.status==='running'?'●':'○'}</span><span>${esc(s.title)}</span><small>${labels[s.status]||'等待中'}</small></li>`).join('')}</ol>${t.status==='running'?'<p class="muted progress-note">数据获取和模型推理可能需要数分钟；四项分析并行执行，完成顺序可能不同。阶段耗时不代表剩余时间，可切换页面继续浏览。连续 15 分钟无阶段进展或总耗时超过 60 分钟将自动停止，可按原配置重试。</p>':''}</div>`;
  }
  function settings() {
    return `<div class="page-head"><div><h1>应用设置</h1><p>模型、凭据与本地存储</p></div></div><div class="panel form-panel"><h2>分析模型默认值</h2><form id="settings-form">${modelFields('settings')}<button class="primary" type="submit">保存默认模型</button></form></div><div class="panel form-panel"><h2>API 凭据</h2><p class="muted">保存在 macOS 钥匙串或 Windows 凭据库。不会写入报告或工作台数据库。也支持已有环境变量与启动目录中的 .env。</p><form id="credential-form"><label>服务商<select name="provider" id="credential-provider">${state.providers.map(p=>`<option value="${esc(p)}" ${p===state.settings.provider?'selected':''}>${esc(p)}</option>`).join('')}</select></label><p id="credential-status" class="muted">点击检查凭据状态。</p><label>API Key<input name="key" type="password" autocomplete="off" spellcheck="false" placeholder="仅输入新凭据时填写" required></label><div class="actions"><button type="submit">保存到系统凭据库</button><button type="button" data-action="credential-check">检查配置</button></div></form><details><summary>本地模型与订阅登录</summary><p class="muted">Ollama 使用本地服务地址和已安装的模型 ID。ChatGPT 使用现有 CLI 登录状态：先运行 tradingagents login chatgpt。Bedrock / Azure 等企业服务仍需相应环境配置。</p></details></div><div class="panel form-panel"><h2>报告自动读取</h2><p class="muted">启动时读取已有报告，每 15 秒检查新增或变更。选择过的目录会保留。</p><div class="report-directories">${state.library.directories.length?state.library.directories.map(path=>`<p class="muted prewrap">${esc(path)}</p>`).join(''):'<p class="muted">当前没有关联目录。</p>'}</div>${state.library.errors.length?`<p class="muted">${state.library.errors.length} 项读取异常，下次检查自动重试。</p>`:''}<div class="actions"><button data-action="import">添加报告目录</button><button data-action="refresh-reports">立即检查</button></div></div><div class="panel form-panel"><h2>本地数据</h2><p class="muted prewrap">${esc(state.data_directory)}</p><p class="muted">包含报告快照、个人记录、任务和运行缓存。备份时关闭应用后复制整个目录。导出报告可在股票档案中操作。</p><div class="actions"><button data-action="theme">切换${state.theme==='dark'?'浅色':'深色'}主题</button></div></div>`;
  }
  function localDate() {const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
  function elapsed(t) {
    const interrupted=t.status==='interrupted';
    if(interrupted&&(!t.last_activity_at||!t.started_epoch))return '耗时未知';
    if(!t.started_epoch)return '';
    const end=interrupted?new Date(t.last_activity_at).getTime():(t.finished_at?new Date(t.finished_at).getTime():Date.now());
    if(!Number.isFinite(end))return '耗时未知';
    const secs=Math.max(0,Math.floor(end/1000-t.started_epoch));
    return `${interrupted?'截至最后活动约耗时':'耗时'} ${Math.floor(secs/60)} 分 ${secs%60} 秒`;
  }
  function renderTask() {
    const t=state.task,el=$('#task-strip');el.hidden=t.status==='idle';el.classList.toggle('running',t.status==='running');
    document.documentElement.style.setProperty('--task-height','0px');
    if(el.hidden){fitPreview();return;}
    const steps=t.progress||[],done=steps.filter(s=>s.status==='completed').length;
    el.innerHTML=`<span><span class="task-dot"></span>${esc(t.ticker)} · ${esc(t.phase||t.status)} <span class="muted">${steps.length?`已完成 ${done}/${steps.length} · `:''}${elapsed(t)}</span></span><div class="actions">${t.status==='completed'?`<button data-report="${esc(t.report_id)}">查看结果</button>`:''}<button data-page="analysis">任务详情</button>${t.status==='running'?'<button data-action="cancel">取消</button>':''}</div>`;
    document.documentElement.style.setProperty('--task-height',`${el.offsetHeight}px`);
    fitPreview();
    if(state.page==='analysis'&&$('#task-detail')){
      $('#task-detail').innerHTML=taskDetail();
      const submit=$('#analysis-form [type="submit"]');if(submit)submit.disabled=taskActive();
    }
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
    if(preview){
      const primary=fields.filter(field=>['mode','execution','invalidation'].includes(field.key));
      const secondary=fields.filter(field=>!['mode','execution','invalidation'].includes(field.key));
      return `<section class="decision-plan preview-plan" aria-label="执行与风险边界"><div class="plan-heading"><h3>执行与风险边界</h3><span>原报告条件</span></div><div class="plan-conditions">${primary.map(item).join('')}</div><details class="preview-details plan-more"><summary>期限、事件与补充依据</summary><div class="plan-conditions">${secondary.map(item).join('')}</div></details></section>`;
    }
    return `<section class="decision-plan ${preview?'':'report-plan'}" aria-label="配置与执行计划"><div class="plan-heading"><h3>配置与执行</h3><span>按原报告条件评估</span></div><div class="plan-metadata">${metadata.map(item).join('')}</div><div class="plan-conditions">${conditions.map(item).join('')}</div><details class="plan-context"><summary>反向证据与市场背景</summary>${context.map(item).join('')}</details></section>`;
  }
  function previewContent(selected){
    const report=state.preview?.id===selected.id?state.preview:null;
    if(!report)return state.previewError?`<div class="preview-empty"><p>${esc(state.previewError)}</p><button data-action="preview-retry">重新读取</button></div>`:'<div class="preview-loading" role="status"><span class="task-dot"></span>正在读取各维度重点…</div>';
    const data=report.preview;
    const source=(item,text,kind='')=>`<button class="preview-point ${kind}" data-preview-source="${esc(item.source)}" data-report-id="${esc(report.id)}" title="查看原文依据"><span>${esc(text||reading(item))}</span><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg></button>`;
    const empty={not_run:'未提供独立分析章节。',review:'该维度校验未通过，请先核查原文。',unstructured:'已有正文，暂未提取到可靠的独立摘要。',insufficient:'现有信息不足以形成该维度判断。',unassessed:'原报告未给出该维度评估。'};
    const angles=data.dimensions.map(angle=>`<section class="preview-angle" data-angle="${esc(angle.key)}"><div class="angle-heading"><h3>${esc(angle.label)}</h3><span class="angle-status ${esc(angle.status)} ${esc(angle.kind)}">${angle.status==='ready'?(angle.kind==='scenario'?'条件情景':angle.kind==='risk'?'风险关注':'重点'):esc(angleStates[angle.status]||'待核查')}</span></div>${angle.items.length?source(angle.items[0]):`<p class="angle-empty">${esc(empty[angle.status]||'暂无可用摘要')}${angle.source?`<button class="link" data-preview-source="${esc(angle.source)}" data-report-id="${esc(report.id)}">查看原文 →</button>`:''}</p>`}${angle.items.length>1?`<details class="preview-details angle-more"><summary>补充依据 · ${angle.items.length-1}</summary>${angle.items.slice(1).map(item=>source(item,reading(item),'supporting-point')).join('')}</details>`:''}</section>`).join('');
    return `${report.warnings.length?`<details class="preview-data-note ${report.rating==='REVIEW'?'needs-review':''}"><summary>${report.rating==='REVIEW'?'报告需复核':'数据说明'} · ${report.warnings.length} 项</summary><ul>${report.warnings.map(text=>`<li>${esc(text)}</li>`).join('')}</ul></details>`:''}<section class="preview-verdict"><h3>研究结论</h3>${data.conclusion?source(data.conclusion):'<p class="muted">暂无独立结论，可打开完整研究核查。</p>'}</section>${decisionPlan(report,true)}<div class="preview-evidence-heading"><h3>关键依据</h3><span>点击摘录查看完整上下文</span></div><div class="preview-angles">${angles}</div><details class="preview-details preview-followup"><summary>后续关注与术语</summary>${data.follow_up?source(data.follow_up):'<p class="angle-empty">原报告未单独列出重新评估条件。</p>'}<p class="preview-glossary">EMA：指数移动平均线；SMA：简单移动平均线；MACD：平滑异同移动平均线；RSI：相对强弱指标；ATR：平均真实波幅；FCF：自由现金流；DPS：每股股息；ARPU：每用户平均收入；PE：市盈率；PB：市净率；ROE：净资产收益率。</p></details>`;
  }
  function fitPreview(){
    const pane=$('#today-detail');if(!pane)return;
    if(getComputedStyle(pane).position==='static'){pane.style.removeProperty('height');return;}
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
  async function act(action, target) {
    if(action.startsWith('strategy-')||action==='strategies-reload'){await strategyAction(action,target);return;}
    if(action==='book-cancel'){
      const text=state.bookText[target.dataset.book];
      try{text.task=await api('cancel_book_text',target.dataset.job);text.error='';}
      catch(error){text.error=error.message;}
      finally{renderBooks();pollBookJobs();}return;
    }
    if(['book-parse','book-resume','book-search','book-citation'].includes(action)){
      const id=target.dataset.book;if(state.bookTextBusy.has(id))return;
      state.bookTextBusy.add(id);
      const text=state.bookText[id]??={};rememberDrafts();text.error='';
      try{
        if(action==='book-parse'||action==='book-resume'){
          if(state.bookParsing.has(id))return;state.bookParsing.add(id);renderBooks();
          text.task=action==='book-resume'?await api('resume_book_text',target.dataset.job):await api('start_book_text',id);pollBookJobs();
        }else if(action==='book-search'){
          const parse=document.querySelector(`[data-book-parse="${id}"]`).value,query=document.querySelector(`[data-book-query="${id}"]`).value;
          delete text.results;delete text.citation;
          const aliases=Object.create(null), lines=document.querySelector(`[data-book-aliases="${id}"]`).value.split(/\r?\n/);
          for(const line of lines){
            if(!line.trim())continue;
            const separator=line.indexOf('=');
            if(separator<1||!line.slice(separator+1).trim())throw new Error('术语别名每行请填写：关键词 = 原文词或短语。');
            const key=line.slice(0,separator).trim(), value=line.slice(separator+1).trim();
            (aliases[key]??=[]).push(value);
          }
          text.results=await api('search_book_text',parse,query,aliases);
        }else text.citation=await api('book_text_citation',target.dataset.parse,Number(target.dataset.ordinal));
      }catch(error){text.error=error.message;}
      finally{state.bookTextBusy.delete(id);state.bookParsing.delete(id);renderBooks();}return;
    }
    if(action==='books-reload'){await loadBooks();return;}
    if(action==='book-verify'){
      const id=target.dataset.book;if(state.bookVerifying.has(id))return;
      state.bookVerifying.add(id);delete state.bookChecks[id];renderBooks();
      try{state.bookChecks[id]=await api('verify_book',id);}
      finally{state.bookVerifying.delete(id);renderBooks();}return;
    }
    if(action==='entry-reload'){await loadEntryPlan();return;}
    if(action==='entry-research'){state.drafts['analysis-ticker']=target.dataset.code;state.drafts['analysis-date']=localDate();go('analysis');return;}
    if(action==='dividend-watch'){
      const code=target.dataset.code;
      state.dividendWatches=await api('save_dividend_watch',code,target.dataset.name);render();
      document.querySelector('.dividend-watches').open=true;
      document.querySelector(`[data-watch-code="${code}"] textarea`)?.focus();return;
    }
    if(action==='dividend-unwatch'){
      state.dividendWatches=await api('remove_dividend_watch',target.dataset.code);
      delete state.drafts[`dividend-note-${target.dataset.code}`];render();toast('已移出自选。');return;
    }
    if(action==='dividend-evidence'){target.nextElementSibling.showModal();return;}
    if(action==='dividend-evidence-close'){target.closest('dialog').close();return;}
    if(action==='dividend-sort'){const key=target.dataset.sort;if(!['yield_pct','market_cap_yi','pe_annual','volatility_1y_pct','volatility_3y_pct','max_drawdown_1y_pct','max_drawdown_3y_pct','turnover_20d_yi'].includes(key))return;const previous=state.dividendSort||{key:'yield_pct',direction:'desc'};state.dividendSort={key,direction:previous.key===key&&previous.direction==='desc'?'asc':'desc'};render();document.querySelector(`[data-sort="${key}"]`)?.focus();return;}
    if(action==='dividend-reset'){state.dividendCriteria={};render();return;}
    if(action==='dividend-filter'){state.dividendFilter=target.dataset.filter;render();return;}
    if(action==='dividend-auto'){state.dividendJob=await api('set_dividend_auto_update',state.dividendJob?.auto_update===false);render();return;}
    if(action==='dividend-reload'){await loadDividends();return;}
    if(action==='entry-environment-refresh'||action==='entry-environment-direct'){
      const asof=state.entryPlan?.as_of;
      const age=asof?Math.floor((Date.parse(localDate())-Date.parse(asof))/86400000):NaN;
      const fresh=age>=0&&age<=4;
      state.dividendJob=await api('refresh_dividends',action==='entry-environment-direct',Boolean(fresh));render();return;
    }
    if(action==='dividend-refresh'||action==='dividend-direct'){
      state.dividendJob=await api('refresh_dividends',action==='dividend-direct');render();return;
    }
    if(action==='dividend-cancel'){state.dividendJob=await api('cancel_dividends');render();return;}
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
      else if(button.dataset.action)await act(button.dataset.action,button);
    } catch(error){toast(error.message,true);}
  });
  document.addEventListener('submit',async event=>{
    if(event.target.id==='dividend-filters'){event.preventDefault();if(!event.target.checkValidity()){event.target.reportValidity();return;}state.dividendCriteria=Object.fromEntries(new FormData(event.target));render();return;}
    event.preventDefault();const form=event.target;if(!form.checkValidity()){showValidation(form);return;}clearValidation(form);
    const submit=form.querySelector('[type=submit]');if(submit)submit.disabled=true;
    try {
      const values=Object.fromEntries(new FormData(form));
      if(form.id==='strategy-form'||form.id==='strategy-confirm-form')await saveStrategyForm(form);
      else if(form.id==='book-form'){
        if(state.bookRegistering)return;
        rememberDrafts();state.bookRegistering=true;
        try{
          const result=await api('register_book',values);
          if(result.cancelled){toast('已取消选择，登记信息已保留。');return;}
          await loadBooks();toast(result.duplicate?'此文件已登记，已保留原记录。':'原文已登记，解析与索引尚未开始。');
        }finally{state.bookRegistering=false;renderBooks();}
      }
      else if(form.id==='entry-preferences-form'){
        await api('save_entry_preferences',values);
        for(const key of ['entry-capital','entry-target','entry-position','entry-horizon'])delete state.drafts[key];
        await loadEntryPlan(false);toast('个人计划已保存。');
      }
      else if(form.classList.contains('dividend-note-form')){
        state.dividendWatches=await api('save_dividend_watch',form.dataset.code,form.dataset.name,values.note);
        delete state.drafts[`dividend-note-${form.dataset.code}`];toast('自选备注已保存。');
      }
      else if(form.id==='analysis-form'){rememberDrafts();state.task=await api('start_analysis',values);renderTask();toast('分析已在后台启动，可继续浏览历史。');}
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
      await pollDividends();
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
    try {const data=await api('bootstrap');Object.assign(state,data,{allReports:data.reports,theme:data.theme||'dark'});document.documentElement.dataset.theme=state.theme;seenTaskStatus=`${state.task.id}:${state.task.status}`;seenLibraryRevision=state.library.revision;render();loadEntryPlan();clearTimeout(pollTimer);poll();}catch(error){$('#connection-note').textContent=error.message;toast(error.message,true);}
  }
  new ResizeObserver(()=>document.documentElement.style.setProperty('--topbar-height',`${$('.topbar').offsetHeight}px`)).observe($('.topbar'));
  window.addEventListener('pywebviewready',start,{once:true});
  if(window.pywebview?.api)start();
  setTimeout(()=>{if(!bridge&&$('#connection-note'))$('#connection-note').textContent='此页面需要桌面应用连接。请运行 tradingagents-desktop，或 python -m tradingagents.desktop。';},2500);
})();
