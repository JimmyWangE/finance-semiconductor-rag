"""Generate labeled PDF fixtures and an isolated index for upload acceptance tests."""
import json
import tempfile
from pathlib import Path

import pymupdf

from semi_rag.config import load_config


def create_fixture(path: Path, scanned=True):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = ['测试报告：仅用于上传功能验收', '发布机构：测试证券', '发布日期：2026年09月07日',
             '芯测科技（测试标的）2026年第二季度营业收入为12.34亿元。',
             '同期归母净利润为2.56亿元，产能利用率为87.6%。',
             '收入增长来自半导体测试设备交付增加。', '研发投入为1.23亿元。',
             '主要风险包括下游需求不及预期、订单交付延期。',
             '验收标识：RAG-UPLOAD-VERIFY-20260907']
    page.insert_text((48, 65), '\n'.join(lines), fontname='china-s', fontsize=14, lineheight=1.8)
    page = doc.new_page(width=595, height=842)
    page.insert_text((45, 55), '测试报告：财务指标表（单位：亿元）', fontname='china-s', fontsize=14)
    rows = [['指标', '2026年第二季度'], ['营业收入', '12.34'], ['归母净利润', '2.56'], ['研发投入', '1.23']]
    for y in [100, 145, 190, 235, 280]:
        page.draw_line((45, y), (540, y), color=(0, 0, 0))
    for x in [45, 290, 540]:
        page.draw_line((x, 100), (x, 280), color=(0, 0, 0))
    for index, row in enumerate(rows):
        page.insert_text((55, 126 + index * 45), row[0], fontname='china-s', fontsize=13)
        page.insert_text((302, 126 + index * 45), row[1], fontname='china-s', fontsize=13)
    page.insert_text((45, 325), '资料来源：功能验收测试数据，不构成真实研究结论。', fontname='china-s', fontsize=13)
    if scanned:
        with pymupdf.open() as raster:
            sheet = raster.new_page(width=595, height=842)
            sheet.insert_text((45, 65), '\n'.join(['扫描页识别测试', '芯测科技测试产线月产能为4.8万片。',
                '本页为2026年第二季度生产数据。', '设备交付周期为6个月。', '以上全部为功能验收数据。']),
                fontname='china-s', fontsize=18, lineheight=2)
            image = sheet.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes('png')
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=image)
    doc.save(path)
    doc.close()


if __name__ == '__main__':
    root = Path(tempfile.mkdtemp(prefix='rag-upload-acceptance-'))
    config = load_config()
    current = (Path(config['index_dir']) / 'current').resolve()
    config['data_dir'] = str(root / 'data')
    config['index_dir'] = str(root / 'index')
    Path(config['index_dir']).mkdir()
    (Path(config['index_dir']) / 'current').symlink_to(current)
    (root / 'config.json').write_text(json.dumps(config, indent=2))
    create_fixture(root / 'upload-fixture.pdf')
    print(root)
