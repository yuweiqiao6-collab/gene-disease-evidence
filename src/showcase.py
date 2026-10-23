"""Offline presentation scenarios; never substitutes for model inference."""
import json
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


def scenarios():
    return json.loads((EXAMPLES / 'showcase_scenarios.json').read_text(encoding='utf-8'))


def scenario(name):
    if not isinstance(name, str) or name not in scenarios():
        raise ValueError('不支持的教学场景。')
    return scenarios()[name]


def render_demo(submit):
    import streamlit as st
    from baseline import PROFILE
    from jobs import Query
    from ui_theme import case_html
    st.subheader('研究流程演示')
    st.caption('人工教学材料 · 预设标注 · 免密钥运行。下列场景用于体验完整流程，不是真实论文或模型能力评测。')
    for number, (column, (name, item)) in enumerate(zip(st.columns(3), scenarios().items()), 1):
        with column:
            with st.container(border=True, key='scenario_' + name):
                st.markdown(case_html(name,item,number),unsafe_allow_html=True)
                if st.button(item['button'], key='showcase_' + name, width='stretch',type='primary' if name=='clear' else 'secondary'):
                    submit(Query((item['gene'],), item['disease'], task_profile=PROFILE, demo_scenario=name))
    st.info('点击一个场景后，向下查看“任务进度与证据结果”。可以核对原句、保存人工复核，再下载表格。')
    with st.expander('查看已有真实调用复盘（历史任务，不会重新调用模型）'):
        review = json.loads((EXAMPLES / 'recorded_run_review.json').read_text(encoding='utf-8'))
        st.write('记录日期：' + review['record_date'] + '；材料：3篇公开摘要；原任务：范围与测量类型提取。')
        st.caption('这是旧任务的开发样本复盘，不能用作当前“疾病与正常对照”任务的准确率。')
        st.write(f"实际请求 {review['requests']} 次，失败 {review['failed_requests']} 次，返回候选 {review['emitted_relations']} 条；服务商记录总用量 {review['usage']['total_tokens']} 词元。")
        for case in review['cases']:
            st.markdown('**' + case['gene'] + ' · 文献编号 ' + case['pmid'] + '**')
            st.write(case['finding'])
            st.caption(case['limitation'])
            st.link_button('查看对应公开文献', case['source_url'], key='recorded_' + case['pmid'])
        st.caption('两条候选的引用均存在，但关系支持范围仍有不足。没有完整独立人工参考，因此不报告医学准确率。')

