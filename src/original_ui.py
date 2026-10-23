"""UI adapter for the repaired original retrieval chain, not a second algorithm."""
import json
import streamlit as st
from baseline import PROFILE
from jobs import Query

METHODS={'bm25':'BM25关键词排序','bm25+dense':'关键词＋向量混合排序',
    'bm25+cross_encoder':'关键词排序＋模型重排序',
    'bm25+dense+cross_encoder':'关键词＋向量混合排序＋模型重排序'}


def render_original(service, submit, *, model_configured=False):
    st.subheader('混合检索与精排：先筛选片段，再提取证据')
    st.caption('本地BM25（关键词评分）粗筛＋PubMed（生物医学文献平台）摘要 → 候选片段向量化或复用缓存 → 关键词与向量融合 → 模型重排序。最后交给LLM（大语言模型）的片段上限按整次查询计算，不是每篇各取同样数量。')
    if service.settings.corpus_db:
        st.caption('两个入口分别使用各自配置的本地索引；页首本地库登记数来自逐篇扫描的关键词索引，不能代表本入口的覆盖范围。')
    if not service.original.ready:
        st.info('混合检索尚未配置。需要BM25关键词索引、本地向量与重排序模型，以及模型运行环境。配置命令见MODEL_SETUP（模型配置指南）；也可选择逐篇证据扫描。')
    with st.form('original_search_form'):
        left,right=st.columns(2)
        gene=left.text_input('基因名称（单个）',value='IL6')
        disease=right.text_input('疾病英文名称',value='rheumatoid arthritis',
            help='rheumatoid arthritis 表示类风湿关节炎；type 2 diabetes 表示2型糖尿病。')
        k=st.select_slider('整次查询最多交给LLM的片段组数',options=[5,10,12,15],value=12)
        pubmed=st.checkbox('补充PubMed（生物医学文献平台）摘要',value=service.settings.enable_literature,
            disabled=not service.settings.enable_literature)
        with st.expander('检索与精排参数'):
            a,b=st.columns(2)
            bm25=a.number_input('本地BM25粗筛上限',10,2000,500,10)
            encode=b.number_input('本次候选向量上限',15,2000,500,5)
            maximum=st.slider('PubMed最多摘要篇数',1,100,50)
            ga=st.text_input('基因别名，英文逗号分隔')
            da=st.text_input('疾病别名，英文逗号分隔')
            st.caption('手动别名优先；每类合计最多10条。')
            auto=st.checkbox('查询公开接口补充别名',value=False,disabled=not service.settings.enable_literature)
            diversity=st.selectbox('每篇最多保留片段组数',[0,1,2,3],format_func=lambda n:'不限制单篇数量' if n==0 else str(n))
        st.caption('首次未命中的片段需要本地编码，可能较慢；重复片段按文字和模型版本复用。服务端限定两个CPU（中央处理器）线程，不使用未分配的显卡。')
        clicked=st.form_submit_button('检索并精排（不调用LLM）',type='primary',disabled=not service.original.ready)
    if clicked:
        aliases=lambda v:tuple(x.strip() for x in v.split(',') if x.strip())
        job=submit(Query((gene.strip(),),disease.strip(),source='original',action='search',top_k=k,
            include_pubmed=bool(pubmed and service.settings.enable_literature),max_papers=maximum,
            gene_aliases=aliases(ga),disease_aliases=aliases(da),task_profile=PROFILE,
            bm25_top_n=int(bm25),encode_top_n=int(encode),auto_lookup_aliases=bool(auto and service.settings.enable_literature),
            per_paper_limit=diversity))
        if job:
            st.session_state.original_target=job;st.session_state.original_consent=False;st.rerun()
    target=st.session_state.get('original_target')
    snapshot=None
    if target:
        try:snapshot=service.snapshot(st.session_state.owner,target)
        except PermissionError:st.info('该精排记录不属于当前研究空间或已删除，请重新检索。')
    ready=bool(snapshot and snapshot['state']=='completed' and snapshot['documents'])
    if snapshot:
        trace=snapshot['provenance'].get('retrieval_trace',{})
        status={'completed':'已完成','running':'正在精排','queued':'排队中','partial':'部分完成','failed':'失败','no_candidates':'没有候选','interrupted':'已中断'}.get(snapshot['state'],snapshot['state'])
        st.write(f"目标：{snapshot['genes'][0]} · {snapshot['disease']}；精排状态：{status}")
        if ready:
            chars=sum(min(9000,len(c['context'])) for c in snapshot['documents'])
            st.info(f"本次最多发送{len(snapshot['documents'])}组片段、正文合计{chars}字符；另含提示和来源信息。每组最多1次请求、不自动重试，每次输出最多2048词元（模型计量文本长度的单位）。")
        if trace:st.caption('实际方法：'+METHODS.get(trace.get('actual_method'),'尚未完成')+'；未入选不代表没有证据。')
    st.caption('抽取任务：疾病状态相对正常对照的表达差异；片段排序分数不直接构成基因与疾病的关系结论。')
    fingerprint=(st.session_state.owner,target,ready)
    if fingerprint!=st.session_state.get('original_consent_scope'):
        st.session_state.original_consent=False;st.session_state.original_consent_scope=fingerprint
    consent=st.checkbox('我允许把上述精排片段发给配置的LLM，理解可能产生费用。',key='original_consent')
    can_extract=service.settings.live_ready and st.session_state.authorized and model_configured
    if st.button('调用LLM抽取这批精排片段',type='primary',disabled=not ready or not consent or not can_extract):
        trace=snapshot['provenance']['retrieval_trace']
        job=submit(Query(tuple(snapshot['genes']),snapshot['disease'],source='original',action='extract',
            original_search_id=target,top_k=trace['final_limit'],consent=True,model_retries=0,task_profile=PROFILE,
            gene_aliases=tuple(trace.get('gene_aliases',[])[:10]),disease_aliases=tuple(trace.get('disease_aliases',[])[:10])))
        if job:st.rerun()


def render_ranked(snapshot):
    trace=snapshot['provenance'].get('retrieval_trace',{})
    st.write('混合检索与精排记录；这里尚未调用LLM（大语言模型）。')
    if trace:
        st.write(f"本地候选{trace.get('local_candidates',0)}组＋在线候选{trace.get('online_candidates',0)}组，最终保留{trace.get('selected_count',0)}组。")
        cache=trace.get('cache',{})
        st.caption(f"片段向量：复用{cache.get('reused',0)}组，新计算{cache.get('encoded',0)}组；实际方法：{METHODS.get(trace.get('actual_method'),'未知')}。")
    rows=snapshot['documents']
    if rows:
        st.dataframe([{'名次':i+1,'论文':c.get('paper_id'),'来源':c.get('source_type'),'位置':c.get('source_location'),
            '关键词分数':c.get('retrieval_score'),'向量分数':c.get('vector_score'),'融合分数':c.get('hybrid_score'),
            '重排序分数':c.get('reranker_score')} for i,c in enumerate(rows)],hide_index=True,width='stretch')
        with st.expander('查看交给LLM之前的原文片段'):
            for i,c in enumerate(rows):st.caption(f"{i+1} · {c.get('paper_id')} · {c.get('source_location','')}");st.text(c['context'])
        st.download_button('下载精排片段与来源记录',json.dumps({'trace':trace,'candidates':rows},ensure_ascii=False,indent=2).encode(),
            file_name='original-ranked-candidates.json',mime='application/json',key='original_ranked_'+snapshot['id'])
