"""Conservative presentation of evidence; no universal biological conclusion."""
from collections import defaultdict

CONDITIONS = ('species', 'population_context', 'tissue_or_cell', 'measurement_type', 'disease_group', 'normal_group')
UNKNOWN = {'', 'unclear', 'unknown', 'not reported', '不明确', '未说明'}


def overview(snapshot):
    rows = snapshot['rows']
    eligible = [r for r in rows if r.get('baseline_status') == 'eligible' and r.get('review_status') != 'rejected']
    confirmed = [r for r in eligible if r.get('review_status') in {'accepted', 'corrected'}]
    pending = [r for r in rows if r.get('baseline_status') == 'needs_scope_review' and r.get('review_status') != 'rejected']
    state = snapshot['state']
    unfinished = state in {'queued', 'running', 'paused', 'partial', 'interrupted', 'failed'}
    if state == 'failed':
        headline = '本次处理未成功，尚不能形成判断'
    elif unfinished:
        headline = '处理尚未完成，当前结果仅供阶段核对'
    elif confirmed:
        headline = f'已有 {len(confirmed)} 条人工核对证据，请按研究条件阅读'
    elif eligible:
        headline = f'找到 {len(eligible)} 条符合范围的候选，等待人工核对'
    elif pending:
        headline = '比较依据仍需核对，暂不判断上下调'
    else:
        headline = '当前材料不足以形成表达方向判断'
    grouped = defaultdict(list)
    for r in confirmed:
        # Missing conditions never imply that two observations are comparable.
        key = tuple(str(r.get(k, 'unclear')).strip() for k in ('gene_or_target', 'disease') + CONDITIONS)
        if any(str(r.get(k, '')).strip().lower() in UNKNOWN for k in CONDITIONS):
            key += (r['evidence_id'],)
        grouped[key].append(r)
    groups = []
    for items in grouped.values():
        first = items[0]
        directions = set(r['expression_direction'] for r in items)
        opposite = {'increased', 'decreased'} <= directions
        complete = all(str(first.get(k, '')).strip().lower() not in UNKNOWN for k in CONDITIONS)
        observations = sorted({(r['expression_direction'], r.get('significance', 'unclear')) for r in items})
        groups.append({**{k: first.get(k, 'unclear') for k in CONDITIONS},
            'directions': sorted(directions), 'evidence_count': len(items),
            'observations': observations,
            'paper_records': len(set(r['paper_id'] for r in items)),
            'evidence_ids': [r['evidence_id'] for r in items],
            'status': '条件缺失，单列待核对' if not complete else (
                '出现相反方向，需核对可比性' if opposite else '按相同字段整理，未判定研究间可比性')})
    return dict(headline=headline, eligible=len(eligible), confirmed=len(confirmed),
        unreviewed=len(eligible)-len(confirmed), scope_pending=len(pending),
        excluded=sum(r.get('baseline_status') == 'out_of_scope' for r in rows),
        rejected=sum(r.get('review_status') == 'rejected' for r in rows),
        unfinished=unfinished, groups=groups)


def render_overview(snapshot, translate):
    import streamlit as st
    from ui_theme import evidence_stats
    result = overview(snapshot)
    with st.container(border=True,key='overview_panel'):
        st.subheader('本次找到什么')
        st.markdown('**' + result['headline'] + '**')
        evidence_stats(result)
        st.caption('范围外 ' + str(result['excluded']) + ' 条；人工拒绝 ' + str(result['rejected']) + ' 条。未检出、处理失败和证据不足都不表示基因与疾病无关。')
        if snapshot.get('synthetic'):
            st.caption('这里是人工教学数据。即使保存“接受”，也只是练习复核操作，不会变成真实科研证据。')
        if result['groups']:
            st.dataframe([{'物种': translate(g['species']), '研究对象': translate(g['population_context']),
                '组织或细胞': g['tissue_or_cell'], '测量对象': translate(g['measurement_type']),
                '疾病组': g['disease_group'], '正常对照': g['normal_group'],
                '观察方向': '、'.join('未见显著差异' if d == 'unchanged' and s == 'not_significant' else translate(d)
                    for d, s in g['observations']),
                '统计说明': '、'.join(sorted({translate(s) for _, s in g['observations']})),
                '证据条数': g['evidence_count'], '论文记录数': g['paper_records'], '说明': g['status']}
                for g in result['groups']], hide_index=True, width='stretch')
            st.caption('仅整理人工接受或修正、且符合范围的记录。字段相同不证明实验可比；论文记录数不等于独立研究数，不按多数票推断统一方向。统计限定请查看证据原句。')
        elif result['eligible']:
            st.caption('先在下方核对候选。保存“接受”或“已修正”后，这里会出现按研究条件整理的已核对记录。')
    return result
