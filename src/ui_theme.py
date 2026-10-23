"""Scientific workbench presentation. Displayed counts come from actual state."""
from html import escape
from pathlib import Path


CSS = '''<style>
:root{--canvas:#f5f7fa;--panel:#ffffff;--panel-raised:#f7f9fc;--line:#dce3ed;--ink:#24364a;--muted:#63758b;--accent:#2563eb}
.stApp{background:var(--canvas);color:var(--ink)}
.block-container{max-width:1360px;padding:3rem 2.5rem 4rem}
h1,h2,h3{color:#24364a!important;letter-spacing:-.025em;font-weight:650!important}
h1{font-size:2rem!important}h2{font-size:1.55rem!important}h3{font-size:1.25rem!important}
[data-testid="stCaptionContainer"]{color:var(--muted);line-height:1.7}
.stCaptionContainer,.stCaptionContainer p,[data-testid="stCaptionContainer"] p{color:var(--muted)!important;opacity:1}
[data-testid="stSidebar"]{background:#f0f3f8;border-right:1px solid #dce3ed}
[data-testid="stSidebar"]>div:first-child{padding-top:1.5rem}
[data-testid="stSidebar"] hr{border-color:#dce3ed;margin:1.25rem 0}
[data-testid="stHeader"]{background:rgba(245,247,250,.96);border-bottom:1px solid rgba(220,227,237,.8)}
.lab-brand{display:flex;gap:12px;align-items:center;padding:8px 0 22px}
.lab-symbol{width:39px;height:39px;display:grid;place-items:center;border:1px solid #c6d8f0;border-radius:10px;background:#eaf0fb;color:#2563eb}
.lab-brand strong{display:block;font-size:19px;letter-spacing:.05em;color:#24364a}
.lab-brand small{display:block;font-size:11px;letter-spacing:.16em;color:#63758b;margin-top:4px}
.science-hero{position:relative;overflow:hidden;display:grid;grid-template-columns:1.5fr 1fr;gap:22px;align-items:center;border:1px solid #d4deec;border-radius:14px;padding:22px 26px;background:radial-gradient(ellipse at 95% 20%,#f1f5ff 0,transparent 55%),linear-gradient(125deg,#ffffff,#ffffff);margin:0 0 16px}
.science-hero:before{content:"";position:absolute;inset:0;background-image:radial-gradient(#a0b4cd 0.65px,transparent 0.65px);background-size:20px 20px;opacity:.09;pointer-events:none}
.hero-copy,.hero-diagram{position:relative;z-index:1}
.hero-kicker{display:flex;gap:12px;align-items:center;font-size:11px;letter-spacing:.1em;color:#63758b;margin-bottom:12px}
.version-tag{font-family:ui-monospace,Consolas,monospace;font-size:11px;border:1px solid #c6d8f0;border-radius:4px;padding:3px 7px;color:#63758b;letter-spacing:0}
.science-hero h1{margin:0 0 12px!important;line-height:1.35;font-size:clamp(24px,2.3vw,33px)!important}
.science-hero p{font-size:14px;color:#63758b;line-height:1.85;margin:0;max-width:610px}
.hero-tags{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.hero-tags span{font-size:11px;border:1px solid #c6d8f0;color:#63758b;background:#f4f7fc;padding:4px 9px;border-radius:4px}
.hero-tags span:first-child{color:#315e9f;border-color:#c6d8f0;background:#edf3ff}
.hero-diagram svg{display:block;width:100%;max-height:150px}
.hero-diagram small{display:block;text-align:right;font-size:10px;letter-spacing:.1em;color:#63758b;margin-top:6px}
.source-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:16px}
.source-tile{border:1px solid var(--line);border-radius:8px;background:#ffffff;padding:13px 16px;min-width:0}
.source-tile .tile-label{font-size:11px;color:#63758b;letter-spacing:.05em;display:flex;align-items:center;gap:7px}
.status-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#63758b;flex-shrink:0}
.status-dot.ready{background:#2563eb;box-shadow:0 0 0 3px #2563eb0d}
.source-tile strong{display:block;font-size:18px;line-height:1.5;margin:6px 0 2px;color:#24364a;font-variant-numeric:tabular-nums}
.source-tile small{display:block;color:#63758b;font-size:10px;line-height:1.55}
.st-key-search_panel,.st-key-documents_panel,.st-key-extraction_panel,.st-key-overview_panel{background:#ffffff!important;border-color:#dce3ed!important}
[data-testid="stVerticalBlockBorderWrapper"]{border-color:var(--line)!important;border-radius:10px!important;background:#ffffff}
[data-testid="stExpander"] details{border-color:#dce3ed;border-radius:7px;background:#ffffff}
[data-testid="stExpander"] summary{font-size:13px;color:#63758b}
[data-testid="stExpander"] summary:hover{color:#1d4ed8;background:#eef2f7}
[data-testid="stTabs"] [role="tablist"]{gap:26px;border-bottom:1px solid #dce3ed;margin-top:8px}
[data-testid="stTabs"] [role="tab"]{font-size:14px;font-weight:600;color:#63758b;padding-bottom:12px}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{color:#1d4ed8}
[data-testid="stTabs"] [data-baseweb="tab-highlight"]{background:#2563eb;height:2px}
.stButton>button,.stDownloadButton>button,.stLinkButton>a{border-radius:6px;border-color:#c6d8f0;background:#ffffff;color:#24364a;min-height:39px;font-size:13px}
.stButton>button:hover,.stDownloadButton>button:hover,.stLinkButton>a:hover{border-color:#2563eb;color:#1d4ed8;background:#edf4ff}
.stButton>button[kind="primary"],.stFormSubmitButton>button[kind="primary"]{background:#2563eb;border-color:#2563eb;color:#ffffff;font-weight:650}
.stButton>button[kind="primary"]:hover,.stFormSubmitButton>button[kind="primary"]:hover{background:#1d4ed8;color:#ffffff}
button[kind^="primary"]{background:#2563eb!important;border-color:#2563eb!important;color:#ffffff!important}
button[kind^="primary"] p{color:#ffffff!important}
button:disabled,button[aria-disabled="true"]{background:#e7edf5!important;border-color:#d5deea!important;color:#73839a!important;cursor:not-allowed!important}
button:disabled p,button[aria-disabled="true"] p{color:#73839a!important}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid #2563eb!important;outline-offset:3px}
[data-testid="stAlert"]{border:1px solid #dce3ed;border-radius:7px}
[data-testid="stAlert"] p{font-size:13px;line-height:1.65}
[data-testid="stMetric"]{background:#f8fafc;border:1px solid #dce3ed;border-radius:8px;padding:15px}
[data-testid="stMetricValue"]{font-family:ui-monospace,Consolas,monospace;color:#245fac}
[data-testid="stDataFrame"]{border:1px solid #dce3ed;border-radius:8px;overflow:hidden}
[data-testid="stText"]{background:#f8fafc;border-left:2px solid #7ca5dc;border-radius:3px;padding:12px 15px;font-size:13px;line-height:1.8}
.case-top{display:flex;align-items:center;justify-content:space-between;color:#63758b;font-size:10px;letter-spacing:.08em;margin-bottom:12px}
.case-kind{border:1px solid #c6d8f0;padding:3px 6px;border-radius:4px;color:#63758b;letter-spacing:0}
.case-symbol{width:32px;height:32px;margin-bottom:12px;border-radius:8px;display:grid;place-items:center;background:#edf3fd;color:#2563eb;border:1px solid #c6d8f0}
.case-symbol svg{width:22px;height:22px}
.case-card h4{color:#24364a;font-size:17px;line-height:1.5;margin:0 0 10px;font-weight:600}
.case-card p{font-size:13px;color:#63758b;line-height:1.85;min-height:64px;margin:0 0 14px}
.case-footer{font-size:11px;color:#63758b;padding-top:12px;border-top:1px solid #dce3ed;line-height:1.8;margin-bottom:12px}
.case-footer b{font-family:ui-monospace,Consolas,monospace;color:#3b6393;font-weight:500}
.st-key-scenario_clear,.st-key-scenario_conditions,.st-key-scenario_insufficient{background:linear-gradient(150deg,#ffffff,#ffffff)!important;border-color:#dce3ed!important;border-top:2px solid #6a8dbe!important;padding:16px!important}
.st-key-scenario_clear{border-top-color:#3b82f6!important}
.st-key-scenario_conditions{border-top-color:#709fe5!important}
.st-key-scenario_insufficient{border-top-color:#bfa576!important}
.evidence-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:16px 0 12px}
.evidence-stat{background:#f8fafc;border:1px solid #dce3ed;border-radius:7px;padding:14px 16px}
.evidence-stat span{display:block;font-size:11px;color:#63758b}
.evidence-stat strong{display:block;font-family:ui-monospace,Consolas,monospace;font-size:30px;line-height:1.35;color:#24364a;margin-top:5px}
.evidence-stat.reviewed strong{color:#1f6e9c}.evidence-stat.pending strong{color:#9a621d}
.evidence-tags{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 13px}
.evidence-tags span{font-size:11px;border:1px solid #c6d8f0;background:#f2f6fc;color:#63758b;border-radius:4px;padding:4px 8px}
.evidence-tags .direction-up{color:#9a621d;border-color:#ead7ba;background:#fff7eb}
.evidence-tags .direction-down{color:#226a93;border-color:#c6dfed;background:#eef7fc}
.comparison-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}
.comparison-cell{background:#f8fafc;border:1px solid #dce3ed;border-radius:6px;padding:12px 14px;min-width:0;overflow-wrap:anywhere}
.comparison-cell small{display:block;font-size:11px;color:#63758b;margin-bottom:5px}
.comparison-cell strong{font-size:13px;font-weight:500;color:#24364a;line-height:1.7}
button,summary{scroll-margin-top:25px}
@media(max-width:1000px){.block-container{padding-left:1.5rem;padding-right:1.5rem}.science-hero{grid-template-columns:1fr}.hero-diagram{display:none}.science-hero h1{font-size:27px!important}}
@media(max-width:650px){.block-container{padding:3.3rem 1rem 3rem}.science-hero{padding:21px 18px}.science-hero h1{font-size:25px!important}.hero-kicker{font-size:10px;gap:8px}.source-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.source-tile{padding:12px}.source-tile strong{font-size:16px}.comparison-grid{grid-template-columns:1fr}.evidence-stats{gap:6px}.evidence-stat{padding:11px 9px}.evidence-stat strong{font-size:25px}.evidence-stat span{font-size:10px}.case-card p{min-height:0}[data-testid="stTabs"] [role="tablist"]{gap:18px}}
@media(prefers-reduced-motion:no-preference){.stButton>button,.stDownloadButton>button{transition:background .15s,border-color .15s}}
</style>'''

FLOW_SVG = '''<svg viewBox="0 0 380 155" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="问题、文献、原句和人工核对的流程示意">
<g fill="none" stroke="#b4c8e4" stroke-width="1"><path d="M48 77H123M165 77H235M277 77H337"/><path d="M143 56V23H256V56M143 98V132H256V98" stroke-dasharray="3 5"/></g>
<g fill="#f5f8ff" stroke="#aac4e7"><rect x="20" y="49" width="56" height="56" rx="10"/><rect x="115" y="49" width="56" height="56" rx="10"/><rect x="228" y="49" width="56" height="56" rx="10"/><rect x="321" y="57" width="36" height="40" rx="7"/></g>
<g fill="none" stroke="#4b7dbb" stroke-width="1.6" stroke-linecap="round"><path d="M37 65L59 89M59 65L37 89M39 69H56M40 77H55M39 85H56"/><path d="M132 63H154V91H132ZM137 70H149M137 76H149M137 82H145"/><path d="M245 69H250V78H243V72M257 69H262V78H255V72M242 87H265"/><path d="M330 77L336 83L348 70"/></g>
<g fill="#63758b" font-size="10" font-family="sans-serif" text-anchor="middle"><text x="48" y="124">研究问题</text><text x="143" y="124">文献检索</text><text x="256" y="124">原句证据</text><text x="339" y="117">复核</text></g>
<g fill="#5e90d7"><circle cx="96" cy="77" r="3"/><circle cx="199" cy="77" r="3"/><circle cx="303" cy="77" r="3"/></g>
</svg>'''


def apply_theme():
    import streamlit as st
    st.markdown(CSS, unsafe_allow_html=True)


def sidebar_brand():
    import streamlit as st
    st.markdown('''<div class="lab-brand"><span class="lab-symbol" aria-hidden="true">◈</span>
<div><strong>证据实验台</strong><small>基因 × 疾病 · 文献研究</small></div></div>''', unsafe_allow_html=True)


def header(info, *, online, configured, authorized, live_ready):
    import streamlit as st
    version = escape((Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip())
    st.markdown('''<div class="science-hero"><div class="hero-copy">
<div class="hero-kicker">生物医学文献证据 · 可追溯研究流程 <span class="version-tag">v''' + version + '''</span></div>
<h1>基因—疾病证据工作台</h1>
<p>检索文献，辨别表达方向，保留物种、组织与比较条件。<br>让每一条上下调判断，都能回到原文核对。</p>
<div class="hero-tags"><span>疾病组 / 正常对照</span><span>原句与来源绑定</span><span>人工复核记录</span></div>
</div><div class="hero-diagram">''' + FLOW_SVG + '''<small>研究工作流示意 · 不表示真实生物学关联</small></div></div>''', unsafe_allow_html=True)
    count = info.get('paper_count')
    try:
        local = f'{int(count):,} 份可检索' if count is not None else ('已接入' if info else '未接入')
    except (ValueError, TypeError):
        local = '已接入' if info else '未接入'
    tiles = [
        ('本地文献索引', local, '可读文本记录，非独立研究数', bool(info)),
        ('PubMed · 生物医学文献', '已启用' if online else '未启用', '开关状态，非接口连通性验证', bool(online)),
        ('大语言模型', '已配置' if configured else '未配置', '仅配置检查，未验证服务可用性', bool(configured)),
        ('本会话抽取权限', '已验证' if authorized else ('待验证口令' if live_ready else '未启用真实抽取'),
            '教学演示无需模型密钥', bool(authorized))]
    html = '<div class="source-grid">'
    for label, value, detail, ready in tiles:
        html += '<div class="source-tile"><span class="tile-label"><i class="status-dot ' + ('ready' if ready else '') + '"></i>' + escape(label) + '</span><strong>' + escape(value) + '</strong><small>' + escape(detail) + '</small></div>'
    st.markdown(html + '</div>', unsafe_allow_html=True)


def case_html(name, item, number):
    icons = {
        'clear': '<path d="M7 18V6m-3 3 3-3 3 3M17 6v12m-3-3 3 3 3-3"/>',
        'conditions': '<path d="M4 6h6v12H4m10-12h6v12h-6M10 12h4"/>',
        'insufficient': '<circle cx="11" cy="10" r="6"/><path d="m16 15 5 5M8 10h6"/>'}
    title = item['title'].split(' · ', 1)[-1]
    return ('<div class="case-card"><div class="case-top"><span>场景 ' + str(number).zfill(2) + '</span><span class="case-kind">人工预设</span></div>'
        '<div class="case-symbol" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + icons[name] + '</svg></div>'
        '<h4>' + escape(title) + '</h4><p>' + escape(item['description']) + '</p>'
        '<div class="case-footer">' + escape(item['gene']) + ' · <b>预设 ' + str(item['expected_evidence']) + ' 条证据</b><br>' + escape(item['learn']) + '</div></div>')


def evidence_stats(result):
    import streamlit as st
    st.markdown('<div class="evidence-stats">' + ''.join(
        '<div class="evidence-stat ' + cls + '"><span>' + label + '</span><strong>' + str(value) + '</strong></div>'
        for cls, label, value in [('pending', '待人工复核', result['unreviewed']),
            ('reviewed', '已人工核对', result['confirmed']), ('', '比较依据待核对', result['scope_pending'])]) + '</div>', unsafe_allow_html=True)


def comparison_groups(disease_group, control_group):
    import streamlit as st
    st.markdown('<div class="comparison-grid">' + ''.join(
        '<div class="comparison-cell"><small>' + label + '</small><strong>' + escape(value or '未给出') + '</strong></div>'
        for label, value in [('疾病组 · 相对于所列对照', disease_group), ('正常对照 · 按原文定义', control_group)]) + '</div>', unsafe_allow_html=True)


def evidence_tags(row, direction_label, translate):
    import streamlit as st
    cls = {'increased': 'direction-up', 'decreased': 'direction-down'}.get(row.get('expression_direction'), '')
    values = [(cls, direction_label), ('', translate(row.get('species', 'unclear'))),
        ('', translate(row.get('measurement_type', 'unclear'))), ('', translate(row.get('review_status', 'unreviewed')))]
    st.markdown('<div class="evidence-tags">' + ''.join('<span class="' + c + '">' + escape(v) + '</span>' for c, v in values) + '</div>', unsafe_allow_html=True)
