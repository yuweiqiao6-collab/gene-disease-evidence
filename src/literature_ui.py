"""Search and collect first; invoke the model only through a separate action."""
import json
import streamlit as st
from jobs import Query
from source_quality import source_flags
from baseline import PROFILE as BASELINE_PROFILE
from scan_ui import render_start as render_scan_start

FULLTEXT_LABELS={'not_requested':'未请求全文','available':'已获取正文段落','unavailable':'暂无可获取开放全文','failed':'全文请求失败'}


def paper_origin(paper):
    providers={s.get('provider') for s in paper.get('sources',[])}
    local='local_corpus' in providers
    online=providers & {'pubmed','europepmc'}
    label='＋'.join(v for k,v in [('pubmed','PubMed'),('europepmc','Europe PMC')] if k in providers)
    return ('本地＋'+label) if local and online else ('本地文献库' if local else label+'在线记录')


@st.fragment(run_every=3)
def render_literature(service,submit,*,model_configured=False):
    settings=service.settings
    engine=st.radio('证据筛选方式',['逐篇证据扫描','混合检索与精排'],
        index=1 if service.original.ready else 0,key='literature_engine',horizontal=True,
        help='混合检索与精排：关键词与向量融合、模型重排序，按整次查询限制片段总数。逐篇证据扫描：关键词检索候选文献，再按每篇预算检查片段。两种方式都可能遗漏证据。')
    if engine=='混合检索与精排':
        from original_ui import render_original
        render_original(service,submit,model_configured=model_configured)
        return
    st.caption('逐篇证据扫描：先用关键词检索候选文献，再按每篇预算检查片段；此入口不做向量融合和模型重排序。扫描范围是当前候选清单，不是整个文献库。')
    if 'lit_target' not in st.session_state:
        previous_record=next((r for r in service.list_history(st.session_state.owner)
            if r['query'].get('action')=='search'),None)
        previous=previous_record['query'] if previous_record else None
        st.session_state.lit_target=({
            'gene':previous['genes'][0],'disease':previous['disease'],
            'gene_aliases':tuple(previous.get('gene_aliases',())),
            'disease_aliases':tuple(previous.get('disease_aliases',())),
            'job_id':previous_record['id'],'skip_reference_like':previous.get('skip_reference_like',False)
        } if previous else None)
    target=st.session_state.lit_target
    with st.container(border=True,key='search_panel'):
        st.subheader('1 · 输入基因与疾病，检索文献')
        st.caption('目标：疾病状态相对正常对照的表达差异。联网查询与本地库互补；检索不调用LLM（大语言模型）。疾病目前使用英文名称，不自动翻译。')
        local_ready=bool(settings.corpus_db and settings.corpus_db.is_file())
        modes=['本地全文库（不联网）','本地全文库＋PubMed补充','仅PubMed在线检索']
        mode=st.radio('检索范围',modes,index=(1 if settings.enable_literature else 0) if local_ready else 2,key='lit_mode',horizontal=True)
        use_local=mode!=modes[2]
        use_online=mode!=modes[0]
        search_ready=(not use_local or local_ready) and (not use_online or settings.enable_literature)
        if use_local and not local_ready:st.info('当前未接入本地库，部署者需配置 RAG_CORPUS_DB；也可切换到仅PubMed检索。')
        if not settings.enable_literature:
            st.info('在线文献检索已关闭。仅本地检索仍可使用；部署者将 RAG_ENABLE_LITERATURE 设为 true（开启）并重启后可使用PubMed补充。')
        with st.form('literature_search'):
            left,right=st.columns(2)
            gene=left.text_input('基因名称（单个）',value=target['gene'] if target else 'IL6',key='lit_gene',placeholder='例如 IL6')
            disease=right.text_input('疾病英文名称',value=target['disease'] if target else 'rheumatoid arthritis',key='lit_disease',
                help='rheumatoid arthritis 表示类风湿关节炎；type 2 diabetes 表示2型糖尿病。')
            maximum=st.slider('每个来源最多返回的候选篇数',min_value=1,max_value=100,value=40,key='lit_maximum')
            with st.expander('检索选项与别名（可选）'):
                ga=st.text_input('检索基因别名，英文逗号分隔',value=', '.join(target['gene_aliases']) if target else '',key='lit_gene_aliases')
                da=st.text_input('检索疾病别名，英文逗号分隔',value=', '.join(target['disease_aliases']) if target else '',key='lit_disease_aliases')
                focused=st.checkbox('聚焦表达或疾病关系词汇（可能漏掉其他相关论文）',key='lit_focused')
                europepmc=st.checkbox('同时检索Europe PMC（欧洲生物医学文献平台）',value=False,disabled=not use_online,key='lit_europepmc')
                refresh=st.checkbox('重新访问接口，不使用 24 小时内的查询缓存',key='lit_refresh')
                skip_references=st.checkbox('跳过明显的参考文献列表片段（本地检索）',value=True,key='lit_skip_references')
                st.caption('仅依据密集引用格式筛选，可能漏判或误判；综述和正文中的背景转述仍需人工核对。关闭后可查看被筛掉的候选。')
                st.caption('RA 等缩写可能有多种含义，请优先填写疾病全称，并核对命中文献。')
            search_clicked=st.form_submit_button('检索候选文献（不调用模型）',type='primary',disabled=not search_ready)
        if search_clicked:
            aliases=lambda text:tuple(v.strip() for v in text.split(',') if v.strip())
            submitted=submit(Query((gene.strip(),),disease.strip(),source='corpus' if use_local else 'pubmed',action='search',
                gene_aliases=aliases(ga),disease_aliases=aliases(da),max_papers=maximum,
                focused=focused,include_local=use_local,include_pubmed=use_local and use_online,refresh=refresh,
                skip_reference_like=skip_references,task_profile=BASELINE_PROFILE,include_europepmc=bool(use_online and europepmc)))
            if submitted:
                st.session_state.lit_target={'gene':gene.strip(),'disease':disease.strip(),
                    'gene_aliases':aliases(ga),'disease_aliases':aliases(da),'job_id':submitted,'skip_reference_like':skip_references}
                st.session_state.lit_selected=[]
                st.session_state.lit_preview_document=None
                st.session_state.lit_collection_scope='本次检索结果'
                st.session_state.lit_model_consent=False
                st.rerun()
        st.caption('上限按来源分别计算，合并后可能超过单个上限；这是返回篇数上限，不是相关性评分或全库容量。进度显示在下方，第二步自动更新候选列表。')

    saved_papers=service.literature.list(st.session_state.owner,limit=1000)
    collection_scope=st.radio('候选文献范围',['本次检索结果','已保存候选（最近200篇）'],key='lit_collection_scope',horizontal=True)
    papers=[]
    if collection_scope=='已保存候选（最近200篇）':
        papers=saved_papers[:200]
    elif target and target.get('job_id'):
        try:
            result=service.snapshot(st.session_state.owner,target['job_id'])
            saved_by_id={p['document_id']:p for p in saved_papers}
            papers=[saved_by_id[p['document_id']] for p in result['documents'] if p['document_id'] in saved_by_id]
            if result['state'] in {'queued','running'}:st.progress(result['progress'],text=result['message'])
            elif result['state'] in {'failed','partial'}:st.warning('本次检索未完整成功，请查看下方任务说明；已有候选仍可核对。')
        except PermissionError:
            st.info('这条检索历史已删除，可重新检索或查看已保存候选。')
    by_id={p['document_id']:p for p in papers}
    previous_selection=st.session_state.get('lit_selected',[])
    if any(d not in by_id for d in previous_selection):
        st.session_state.lit_selected=[d for d in previous_selection if d in by_id]
    if st.session_state.get('lit_preview_document') not in by_id:
        st.session_state.lit_preview_document=None
    modes=['自动分批扫描全部当前候选','手动抽取少量文献']
    mode_key='lit_process_mode_'+(target['job_id'] if target else 'empty')
    with st.container(border=True,key='documents_panel'):
        st.subheader('2 · 查看候选文献，确认处理方式')
        st.caption(f'当前范围共{len(papers)}篇候选。它是查询命中的候选清单，不是整个本地文献库；保存候选不代表已确认相关。')
        if papers:
            st.dataframe([{'标题':p['title'],'材料来源':paper_origin(p),
                '文本匹配':{'gene_and_disease':'文内同时命中基因与疾病','gene_only':'仅基因命中：扩展候选'}.get(p.get('local_document_match'),'标题/摘要检索'),
                '本地可读全文':'有' if p.get('local_text_available') else '无',
                '摘要':'有' if p.get('abstract') else '无'} for p in papers],hide_index=True,width='stretch',height=min(350,38+35*len(papers)))
        else:
            st.info('当前列表暂无候选。先完成检索；已有记录可切换到“已保存候选”，或在侧栏恢复研究空间。')
        mode=st.radio('处理方式',modes,index=0,key=mode_key,horizontal=True)
        mode_revision=(mode_key,mode)
        if mode_revision!=st.session_state.get('lit_previous_process_mode'):
            st.session_state.lit_model_consent=False
            st.session_state.scan_start_consent=False
            st.session_state.lit_previous_process_mode=mode_revision
        selected=[]
        if mode==modes[0]:
            st.info(f'批量扫描范围：当前{len(papers)}篇候选，无需逐篇勾选。本轮实际处理篇数与请求预算在第三步设置，未完成部分可继续。')
            if papers:
                with st.expander('预览文献或单独获取开放全文（可选）'):
                    preview=st.selectbox('选择一篇预览（不改变批量扫描范围）',list(by_id),index=None,
                        format_func=lambda d:paper_origin(by_id[d])+' · '+by_id[d]['title'],
                        key='lit_preview_document',placeholder='按需选择一篇查看摘要、来源或获取正文')
                    selected=[preview] if preview else []
                    render_paper_preview(service,submit,settings,target,selected,by_id)
        elif papers:
            selected=st.multiselect('选择本次手动抽取的文献（最多5篇）',list(by_id),max_selections=5,
                format_func=lambda d:paper_origin(by_id[d])+' · '+by_id[d]['title'],key='lit_selected',
                placeholder='选择本次要单独处理的文献',select_all=False)
            st.caption(f'手动抽取只处理所选{len(selected)}篇；需要扫描全部候选时切换为自动分批扫描。')
            render_paper_preview(service,submit,settings,target,selected,by_id)
        if mode==modes[1] and list(selected)!=st.session_state.get('lit_previous_selection',[]):
            st.session_state.lit_model_consent=False
            st.session_state.lit_previous_selection=list(selected)
        if any(p.get('local_document_match')=='gene_only' for p in papers):
            st.caption('扩展候选只命中基因词，可能研究其他疾病；共同出现也不证明关系成立，请核对证据。')

    with st.container(border=True,key='extraction_panel'):
        st.subheader('3 · 确认目标，调用模型整理证据')
        st.caption('这一步会把候选文本发送到配置的模型服务，可能产生费用；检索和获取正文不会自动触发它。')
        if target:
            st.write('本次抽取目标：'+target['gene']+' · '+target['disease'])
            st.caption('目标来自最近已提交的文献检索。仅编辑第一步输入框不会改变它；更换目标请重新提交检索。')
        else:
            st.info('请先提交第一步检索，明确本次基因和疾病目标。')
        scope=st.radio('抽取证据范围',['auto','abstract'],format_func=lambda v:'本地可读全文、已获取正文与摘要' if v=='auto' else '仅使用摘要',key='lit_scope',horizontal=True)
        can_extract=settings.live_ready and st.session_state.authorized and model_configured
        if not model_configured:
            st.info('模型尚未配置：自行部署者可在私有 .env 中填写自己的模型密钥、地址和名称，详见侧栏“使用自己的模型密钥”。')
        if not settings.live_ready:
            st.info('真实抽取尚未启用：部署者需开启 RAG_ENABLE_LIVE，并设置至少12字符的访问口令。')
        elif not st.session_state.authorized:
            st.info('请在侧栏输入“真实抽取访问口令”；它不是模型密钥。')
        if mode==modes[0]:
            render_scan_start(service,submit,papers,target,scope,can_extract)
        else:
            render_quick(submit,settings,target,selected,by_id,scope,can_extract)

    if papers:
        with st.expander('文献收藏管理'):
            st.caption('删除收藏不会删除历史任务内已经保存的结果；公共接口缓存会按缓存规则保留。')
            confirm=st.checkbox('我确认删除所选文献的私有收藏',key='lit_delete_confirm')
            if st.button('删除所选收藏',disabled=not selected or not confirm):
                for document_id in selected: service.literature.delete(st.session_state.owner,document_id)
                st.rerun()



def render_paper_preview(service,submit,settings,target,selected,by_id):
    with st.expander('开放正文获取选项'):
        refresh_fulltext=st.checkbox('重新核对已获取的全文',key='lit_refresh_fulltext')
        st.caption('通过Europe PMC（欧洲生物医学文献平台）获取可用的开放正文段落；并非每篇论文都有可获取全文。')
    resolvable=any(by_id[d].get('pmid') or by_id[d].get('pmcid') or by_id[d].get('doi') for d in selected)
    if selected and not resolvable:st.caption('所选本地论文已有命中页码文本；缺少可核验的公开编号，未尝试按标题猜测或获取在线全文。')
    if st.button('获取所选开放全文（不调用模型）',disabled=not selected or not target or not settings.enable_literature or not resolvable):
        submitted=submit(Query((target['gene'],),target['disease'],source='library',action='fulltext',
            document_ids=tuple(selected),refresh=refresh_fulltext))
        if submitted: st.rerun()
    for p in [by_id[d] for d in selected]:
        with st.expander(p['paper_id']+' · '+p['title'],expanded=len(selected)==1):
            st.write('摘要：'+('可读取' if p.get('abstract') else '未获取或原记录无摘要')+'；'+FULLTEXT_LABELS.get(p.get('fulltext_status'),'未知全文状态'))
            if 'preprint' in source_flags(p):st.caption('来源标为预印本：正式发表前的论文版本，需核对后续版本与研究质量。')
            if p.get('source_url'): st.link_button('查看文献来源',p['source_url'],key='source_'+p['document_id'])
            if p.get('abstract'): st.text(p['abstract'])
            if p.get('fulltext_message'): st.caption(p['fulltext_message'])
            if p.get('fulltext_license'): st.caption('原文许可声明：'+p['fulltext_license'])
            if p.get('fulltext_last_attempt'): st.warning('最近一次更新全文失败，保留之前已核对的正文。')
            if p.get('fulltext_source_url'): st.link_button('打开全文记录',p['fulltext_source_url'],key='fullsource_'+p['document_id'])
            detail=service.literature.get(st.session_state.owner,p['document_id'])
            if detail.get('local_chunks'):
                st.caption('已保存的本地片段预览，可能来自历史检索；模型抽取会按当前目标重新检索所选论文的全部已索引文本。')
                previews=detail['local_chunks']
                if target and target.get('skip_reference_like'):
                    previews=[c for c in previews if 'reference_like' not in source_flags(c)]
                for c in previews[:3]:
                    flags=source_flags(c)
                    if 'reference_like' in flags:st.warning('这段具有参考文献列表格式，不能直接当作该论文的研究结果。')
                    if 'review_title' in flags:st.caption('标题提示这可能是综述或汇总研究，需追溯它引用的原始研究。')
                    st.caption(c.get('source_location','本地来源片段'));st.text(c['context'])
            if detail.get('fulltext_chunks'):
                with st.expander('查看已获取的正文段落（前 20 条）'):
                    for c in detail['fulltext_chunks'][:20]:
                        st.caption(c['source_location']);st.text(c['context'])
            st.download_button('下载该文献与来源记录',json.dumps(detail,ensure_ascii=False,indent=2).encode(),
                file_name=p['paper_id']+'_literature.json',mime='application/json',key='paper_'+p['document_id'])

def render_quick(submit,settings,target,selected,by_id,scope,can_extract):
        readable_scope=scope!='abstract' or any(by_id[d].get('abstract') for d in selected)
        if selected and not readable_scope:st.info('所选本地材料没有单独摘要，请选择包含本地可读全文的范围。')
        maximum=min(settings.max_top_k,settings.max_model_chunks//max(1,len(selected)))
        top_k=st.slider('每篇最多抽取的候选片段数',min_value=1,max_value=maximum,value=min(2,maximum),key='lit_top_k',
            help='每篇论文独立分配片段额度，整次任务最多24组；不是把所有全文直接发送。')
        retry=st.checkbox('模型请求失败后自动重试1次（可能增加费用）',value=False,key='lit_retry')
        request_limit=len(selected)*int(top_k)*(2 if retry else 1)
        st.caption(f'本次最多发起{request_limit}次模型请求，每次输出上限2048词元（模型计量文本长度的单位）；缓存命中或候选不足时会更少。实际金额由模型服务商计费。')
        st.caption('每篇单独检索和分配额度；没有可读或匹配文字的论文会列明，不能当作已完成全文检查。')
        st.caption('抽取范围固定为疾病组相对正常对照；候选会按需补充同篇相邻上下文，每组最多9000字符。缺少比较原句的输出进入待核对区。')
        if not selected: st.caption('还需在第二步选择1–5篇文献。')
        payload_settings=(BASELINE_PROFILE,json.dumps(target,sort_keys=True),tuple(selected),scope,int(top_k),bool(retry),
            tuple((d,by_id[d].get('local_snapshot_sha256'),by_id[d].get('abstract_sha256'),by_id[d].get('fulltext_sha256')) for d in selected))
        if payload_settings!=st.session_state.get('lit_payload_settings'):
            st.session_state.lit_model_consent=False
            st.session_state.lit_payload_settings=payload_settings
        consent=st.checkbox('我允许将所选候选文本发送到配置的模型服务。',key='lit_model_consent')
        if st.button('调用模型整理所选文献',type='primary',
                disabled=not selected or not target or not consent or not can_extract or not readable_scope):
            submitted=submit(Query((target['gene'],),target['disease'],source='library',action='extract',
                document_ids=tuple(selected),evidence_scope=scope,top_k=int(top_k),consent=consent,
                gene_aliases=target['gene_aliases'],disease_aliases=target['disease_aliases'],model_retries=int(retry),
                skip_reference_like=target.get('skip_reference_like',False),task_profile=BASELINE_PROFILE))
            if submitted: st.rerun()

def render_documents(snapshot,service,active):
    st.caption('这是文献检索或全文获取记录；没有调用模型，也没有生成基因—疾病关系结论。')
    pubmed=snapshot['provenance'].get('pubmed',{})
    if 'matched_count' in pubmed:
        st.write(f"PubMed 匹配 {pubmed['matched_count']} 篇，本次获取 {pubmed['retrieved_count']} 篇。")
        if pubmed.get('truncated_by_limit'): st.warning('已达到本次篇数上限；当前列表不是完整检索结果。')
        st.caption('本次使用缓存' if pubmed.get('cache_hit') else '本次记录来自接口请求')
    docs=snapshot['documents']
    ep=snapshot['provenance'].get('europepmc',{})
    if 'matched_count' in ep:st.write(f"Europe PMC匹配{ep['matched_count']}篇，本次获取{ep['retrieved_count']}篇，来源编号一致时合并。")
    if 'local_documents' in snapshot['provenance']:
        st.write('本地检索返回'+str(snapshot['provenance']['local_documents'])+'篇候选；本地数量按论文记录计算，不是片段数。')
        st.caption('同篇来源只按已知且一致的编号合并；缺少可核验编号时保留独立记录。标题相似不会自动合并。')
    st.dataframe([{'论文编号':p['paper_id'],'标题':p['title'],'材料来源':paper_origin(p),'摘要':bool(p.get('abstract')),
        '全文状态':FULLTEXT_LABELS.get(p.get('fulltext_status'),'未请求全文')} for p in docs],hide_index=True,width='stretch')
    st.caption('回到“真实文献与模型抽取”页签确认处理方式，默认批量扫描无需选文献；可展开预览阅读摘要或获取正文，再进入第三步抽取；收藏区每3秒更新。')
    st.download_button('下载本次文献检索记录',service.download(st.session_state.owner,active,'literature.json'),
        file_name=active[:8]+'_literature.json',mime='application/json',key='literature_export_'+active)
