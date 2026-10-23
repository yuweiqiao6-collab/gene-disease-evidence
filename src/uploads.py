from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from schemas import ChunkRecord
from text_utils import clean_text, split_sentences


@dataclass(frozen=True)
class Upload:
    name: str
    data: bytes


def validate_uploads(uploads, settings):
    if len(uploads) > settings.max_uploads:
        raise ValueError(f'最多上传 {settings.max_uploads} 个文件。')
    total = 0
    for upload in uploads:
        name = upload.name.replace('\\', '/').split('/')[-1]
        if not name.lower().endswith('.pdf') or not upload.data.startswith(b'%PDF-'):
            raise ValueError('只支持具有有效 PDF 文件头的论文文件。')
        if len(upload.data) > settings.max_upload_mb * 1024 * 1024:
            raise ValueError(f'每个文件不能超过 {settings.max_upload_mb} MB。')
        total += len(upload.data)
    if total > settings.max_uploads * settings.max_upload_mb * 1024 * 1024:
        raise ValueError('上传总大小超限。')


def save_and_parse(uploads, directory: Path, settings):
    import fitz
    validate_uploads(uploads, settings)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = []
    for upload in uploads:
        name = upload.name.replace('\\', '/').split('/')[-1][:160]
        paper_id = 'UPLOAD-' + uuid4().hex
        path = directory / (paper_id + '.pdf')
        path.write_bytes(upload.data)
        try:
            with fitz.open(path) as doc:
                if doc.is_encrypted:
                    raise ValueError('不支持加密论文，请先解密后上传。')
                if len(doc) > settings.max_pages:
                    raise ValueError(f'每篇论文最多 {settings.max_pages} 页。')
                characters = 0
                for page_number, page in enumerate(doc, 1):
                    text = clean_text(page.get_text('text'))
                    characters += len(text)
                    if characters > 1_000_000:
                        raise ValueError('单篇论文文本过长，请拆分后上传。')
                    sentences = split_sentences(text)
                    for i, sentence in enumerate(sentences):
                        context = ' '.join(sentences[max(0, i-1):i+2])
                        record = ChunkRecord(f'{paper_id}_p{page_number}_s{i}', paper_id, name,
                            'uploaded_pdf', page_number, 'unknown', sentence, context,
                            f'{name}，第 {page_number} 页', str(doc.metadata.get('title') or name)).to_dict()
                        records.append(record)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('论文无法解析，请检查文件是否损坏。') from exc
    if not records:
        raise ValueError('没有提取到可读文本；扫描论文需要先识别文字。')
    return records
