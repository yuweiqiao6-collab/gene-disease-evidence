"""Chinese full-reference workbook backed by the existing strict gold importer."""
from __future__ import annotations
import argparse
import json
import sys
import tempfile
from pathlib import Path
from openpyxl import Workbook,load_workbook
from openpyxl.styles import Alignment,Font,PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
sys.path.insert(0,str(Path(__file__).resolve().parent))
from annotation_sheet import export_sheet,import_sheet,read_cases,CASE_COLUMNS,EXTENDED_RELATION_COLUMNS,DISEASE_LABELS

CASE_HEADERS=['案例编号','论文编号','目标基因','目标疾病英文','固定分组','来源链接','核对用原文',
              '完成状态','是否有本任务可用证据','明确确认没有符合范围的关系','复核人','备注']
REL_HEADERS=['案例编号','目标基因','目标疾病英文','证据原句（从原文复制）','表达变化','疾病关系',
             '关系类型','物种','研究类型（可选）','组织或细胞（可选）','关系范围','研究对象','测量对象']
OPTIONS={
 'expression_direction':{'升高':'increased','降低':'decreased','未变化':'unchanged','不明确':'unclear'},
 'disease_effect':{'风险增加':'risk_increase','风险降低':'risk_decrease','进展加重':'progression_increase',
   '进展减轻':'progression_decrease','预后较差':'poor_prognosis','预后较好':'better_prognosis',
   '仅表达差异或标志物':'biomarker_only','未发现所述关联':'no_relation','不明确':'unclear'},
 'relation_type':{'相关性':'associated','实验支持的因果关系':'causal','机制证据':'mechanistic',
   '预测关联':'predictive','治疗靶点':'therapeutic_target','不明确':'unclear'},
 'species':{'人':'human','小鼠':'mouse','大鼠':'rat','其他物种（请在论文备注写明）':'other','不明确':'unclear'},
 'relation_scope':{'直接支持目标关系':'direct','间接关系':'indirect','背景说明':'background','不明确':'unclear'},
 'population_context':{'疾病研究人群':'disease_cohort','仅健康人':'healthy_only','动物疾病模型':'animal_disease_model',
   '细胞疾病模型':'cell_disease_model','混合人群或模型':'mixed','不明确':'unclear'},
 'measurement_type':{'转录本数量':'transcript','总蛋白数量':'total_protein','循环蛋白浓度':'circulating_protein',
   '膜表面丰度':'surface_abundance','定位或转运':'localization','活性':'activity','不明确':'unclear'}}

SOURCE_HEADERS=['案例编号','目标基因','目标疾病（英文与中文）','论文标题','来源链接','本次核对原文（以此为准）']

def source_rows(cases):
    return [[c['case_id'],c['gene'],c['disease']+'（'+DISEASE_LABELS.get(c['disease'],'疾病名称')+'）',
             c.get('title',''),c.get('source_url',''),c['context']] for c in cases]

def text_cells(book):
    for sheet in book:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'

def export_chinese(cases,output):
    if output.exists():raise ValueError('表格已存在，请换一个文件名，避免覆盖人工工作。')
    if any(len(c['context'])>32767 for c in cases):raise ValueError('原文超过单元格容量，不能截断后交给人工确认。')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        source=Path(temporary)/'reference.xlsx'
        export_sheet(cases,source,profile='scope_measurement')
        book=load_workbook(source)
    case_sheet=book['案例'];case_sheet.title='1论文判断'
    relation_sheet=book['关系'];relation_sheet.title='2关系答案'
    for i,value in enumerate(CASE_HEADERS,1):case_sheet.cell(1,i,value)
    for i,value in enumerate(REL_HEADERS,1):relation_sheet.cell(1,i,value)
    case_sheet.data_validations.dataValidation=[]
    relation_sheet.data_validations.dataValidation=[]
    def dropdown(sheet,col,choices):
        rule=DataValidation(type='list',formula1='"'+','.join(choices)+'"',allow_blank=True)
        rule.showErrorMessage=True;rule.error='请从中文下拉选项选择。';rule.errorTitle='选项不匹配'
        sheet.add_data_validation(rule);rule.add(f'{col}2:{col}5000')
    for col,choices in [('H',['待填写','已完成']),('I',['有','没有']),('J',['是','否'])]:dropdown(case_sheet,col,choices)
    for row in case_sheet.iter_rows(min_row=2):
        row[7].value='已完成' if row[7].value=='已确认' else '待填写'
        row[8].value={'相关':'有','不相关':'没有'}.get(row[8].value,'')
        row[0].hyperlink=f"#'3原文与来源'!A{row[0].row}"
    from openpyxl.utils import get_column_letter
    for field,mapping in OPTIONS.items():
        col=EXTENDED_RELATION_COLUMNS.index(field)+1
        dropdown(relation_sheet,get_column_letter(col),list(mapping))
        reverse={v:k for k,v in mapping.items()}
        for row in relation_sheet.iter_rows(min_row=2):
            cell=row[col-1]
            if cell.value:cell.value=reverse.get(cell.value,cell.value)
    guide=book['填写说明'];book.remove(guide)
    guide=book.create_sheet('先看怎么填',0)
    originals=book.create_sheet('3原文与来源');originals.append(SOURCE_HEADERS)
    for row in source_rows(cases):originals.append(row)
    for col,width in {'A':25,'B':12,'C':40,'D':55,'E':45,'F':110}.items():originals.column_dimensions[col].width=width
    originals.freeze_panes='C2'
    for row in originals:
        for cell in row:cell.alignment=Alignment(wrap_text=True,vertical='top')
        if row[0].row>1:originals.row_dimensions[row[0].row].height=300
    notes=[
      ('任务','判断当前基因在当前疾病或疾病模型中的表达/丰度证据；基因变异关联不是表达变化。'),
      ('核对材料','这份表只根据“核对用原文”填写；若为摘要，只按摘要判断，不能混入全文中才有的信息。'),
      ('查看原文','在“1论文判断”点击案例编号，跳到“3原文与来源”。长原文单独显示，避免把待填写列挤出屏幕。'),
      ('先填论文判断','每篇只填黄色区域：有无本任务可用证据、是否明确无符合关系、复核人、备注；全部检查完再选已完成。'),
      ('再填关系答案','有可用证据时，复制原句并选择七项：表达变化、疾病关系、关系类型、物种、关系范围、研究对象、测量对象。'),
      ('有多条结果','可以复制该案例的整行追加；保留原案例编号、目标基因和疾病，每条关系单独填。'),
      ('没有符合关系','论文判断选“没有”，明确无符合范围的关系选“是”，关系答案只保留前三列，后面留空。没有抽取证据不代表生物学上没有关联。'),
      ('原文不足或拿不准','保持待填写，在备注写清缺少的信息；不要为交表而选择不明确或没有。可确认某字段确实未交代时，才在该字段选不明确。'),
      ('表达与疾病方向','表达升高不直接等于风险增加，也不直接等于抑制这个基因能治疗。只按原文填写。'),
      ('研究对象与测量','小鼠/细胞不能写成人群；血中蛋白、膜表面丰度、活性与转录本是不同测量。'),
      ('物种下拉','常见物种可直接选择；其他物种可直接填写原文的英文物种名，并在备注解释中文。'),
      ('七项之外','研究类型、组织或细胞为可选文字；比较条件和判断理由可写在论文备注中。'),
      ('灰色区域','编号、基因、疾病、来源、分组和核对原文不可修改；导入会逐项比对。'),
      ('测试纪律','不提供模型答案；独立测试答案不能用于调提示后又宣称同一组是独立检验。'),
    ]
    for row in notes:guide.append(row)
    guide.column_dimensions['A'].width=25;guide.column_dimensions['B'].width=105
    for row in guide:
        row[1].alignment=Alignment(wrap_text=True,vertical='top');guide.row_dimensions[row[0].row].height=52
    for sheet,fixed in [(case_sheet,7),(relation_sheet,3)]:
        for row in sheet:
            for cell in row:
                cell.alignment=Alignment(wrap_text=True,vertical='top')
                cell.fill=PatternFill('solid',fgColor='E8EDF2' if cell.column<=fixed else 'FFF2CC')
        for cell in sheet[1]:
            cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='235B6C')
        sheet.freeze_panes='C2' if fixed==7 else 'D2'
        sheet.auto_filter.ref=sheet.dimensions
        for row in range(2,sheet.max_row+1):sheet.row_dimensions[row].height=100
    case_sheet.column_dimensions['G'].width=90
    for col in ['E','F','G']:case_sheet.column_dimensions[col].hidden=True
    case_sheet.column_dimensions['A'].width=25;case_sheet.column_dimensions['D'].width=30
    for col in ['H','I','J','K','L']:case_sheet.column_dimensions[col].width=24
    for col in ['E','F','G','H','K','L','M']:relation_sheet.column_dimensions[col].width=24
    guide.append(('疾病中文对照','；'.join(f'{k} 表示 {v}' for k,v in DISEASE_LABELS.items())))
    text_cells(book);book.save(output)

def import_chinese(cases,path,output):
    if output.exists():raise ValueError('输出已存在，请使用新文件名。')
    book=load_workbook(path,data_only=False)
    if not {'1论文判断','2关系答案','3原文与来源'}<=set(book.sheetnames):raise ValueError('不是完整中文参考表或缺少原文页。')
    source=book['3原文与来源']
    if [c.value for c in source[1]]!=SOURCE_HEADERS or list(source.iter_rows(min_row=2,values_only=True))!=[tuple(v or None for v in row) for row in source_rows(cases)]:
        raise ValueError('展示的原文或来源已修改，不能导入。')
    sheets=[book['1论文判断'],book['2关系答案']]
    if [c.value for c in sheets[0][1]]!=CASE_HEADERS or [c.value for c in sheets[1][1]]!=REL_HEADERS:
        raise ValueError('表头被修改，请使用原模板。')
    if any(c.data_type=='f' for s in sheets for row in s for c in row):raise ValueError('参考表不能包含公式。')
    for i,value in enumerate(CASE_COLUMNS,1):sheets[0].cell(1,i,value)
    for i,value in enumerate(EXTENDED_RELATION_COLUMNS,1):sheets[1].cell(1,i,value)
    for row in sheets[0].iter_rows(min_row=2):
        status=row[7].value
        if status not in {'待填写','已完成','',None}:raise ValueError('未知完成状态。')
        row[7].value='已确认' if status=='已完成' else '待标注'
        if row[8].value not in {'有','没有','',None}:raise ValueError('请明确选择有或没有；拿不准请保持待填写。')
        row[8].value={'有':'相关','没有':'不相关'}.get(row[8].value,'')
    for row in sheets[1].iter_rows(min_row=2):
        for field,mapping in OPTIONS.items():
            cell=row[EXTENDED_RELATION_COLUMNS.index(field)]
            if cell.value in (None,''):continue
            if cell.value in mapping:cell.value=mapping[cell.value]
            elif field!='species':raise ValueError('关系答案需从中文选项选择。')
            if cell.value=='other':raise ValueError('其他物种请直接填写原文中的物种名，不能只选其他。')
    sheets[0].title='案例';sheets[1].title='关系'
    with tempfile.TemporaryDirectory() as temporary:
        converted=Path(temporary)/'converted.xlsx';text_cells(book);book.save(converted)
        return import_sheet(cases,converted,output)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='中文完整参考表：空白与未确认项目不会充当人工答案')
    parser.add_argument('action',choices=['export','import'])
    parser.add_argument('--annotations',type=Path,required=True)
    parser.add_argument('--workbook',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();cases=read_cases(args.annotations)
    if args.action=='export':export_chinese(cases,args.workbook)
    elif args.output is None:parser.error('导入时需要 --output 指定新文件')
    else:print('人工确认案例数：',import_chinese(cases,args.workbook,args.output))
