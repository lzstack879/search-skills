#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成知识库产品文档：PDF产品介绍和Excel价格表
"""

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

def generate_pdf():
    """生成产品介绍PDF"""
    pdf_file = "docs/知识库产品介绍.pdf"

    # 创建PDF文档
    doc = SimpleDocTemplate(pdf_file, pagesize=A4,
                          leftMargin=2*cm, rightMargin=2*cm,
                          topMargin=2*cm, bottomMargin=2*cm)

    # 尝试注册中文字体（如果系统有的话）
    try:
        # Windows中文字体
        font_paths = [
            'C:/Windows/Fonts/msyh.ttc',  # 微软雅黑
            'C:/Windows/Fonts/simsun.ttc',  # 宋体
            'C:/Windows/Fonts/simhei.ttf',  # 黑体
        ]
        for font_path in font_paths:
            if os.path.exists(font_path):
                pdfmetrics.registerFont(TTFont('Chinese', font_path))
                break
        font_name = 'Chinese'
    except:
        # 如果中文字体不可用，使用默认字体
        font_name = 'Helvetica'

    # 定义样式
    styles = getSampleStyleSheet()

    # 标题样式
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontName=font_name,
        fontSize=24,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=30,
        alignment=TA_CENTER
    )

    # 一级标题样式
    h1_style = ParagraphStyle(
        'CustomH1',
        parent=styles['Heading1'],
        fontName=font_name,
        fontSize=18,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=12,
        spaceBefore=20
    )

    # 二级标题样式
    h2_style = ParagraphStyle(
        'CustomH2',
        parent=styles['Heading2'],
        fontName=font_name,
        fontSize=14,
        textColor=colors.HexColor('#34495e'),
        spaceAfter=10,
        spaceBefore=15
    )

    # 正文样式
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['BodyText'],
        fontName=font_name,
        fontSize=10,
        textColor=colors.HexColor('#333333'),
        spaceAfter=8,
        leading=16
    )

    # 构建文档内容
    story = []

    # 主标题
    story.append(Paragraph("知识库产品介绍", title_style))
    story.append(Spacer(1, 0.5*cm))

    # 产品概述
    story.append(Paragraph("产品概述", h1_style))
    story.append(Paragraph(
        "智能知识库是一个企业级知识管理平台，帮助团队高效地创建、组织、共享和检索知识内容。"
        "通过AI技术赋能，让知识沉淀更简单，协作更顺畅。",
        body_style
    ))
    story.append(Spacer(1, 0.3*cm))

    # 核心功能
    story.append(Paragraph("核心功能", h1_style))

    features = [
        ("知识管理", [
            "多格式支持：支持 Markdown、富文本、PDF、Word、图片等",
            "智能分类：自动标签推荐和知识图谱构建",
            "版本控制：完整的文档历史记录和版本对比",
            "模板系统：内置常用文档模板"
        ]),
        ("智能搜索", [
            "全文检索：毫秒级响应的全文搜索引擎",
            "语义搜索：基于AI的语义理解",
            "搜索推荐：根据使用习惯智能推荐",
            "高级筛选：多维度筛选功能"
        ]),
        ("协作功能", [
            "实时协作：多人同时编辑，实时同步",
            "评论讨论：支持文档内评论和讨论串",
            "权限管理：灵活的角色和权限控制",
            "审批流程：自定义工作流"
        ]),
        ("AI 增强", [
            "智能问答：基于知识库的AI问答助手",
            "自动摘要：自动生成文档摘要",
            "内容推荐：智能推荐相关文档",
            "质量检测：自动检测文档质量"
        ])
    ]

    for title, items in features:
        story.append(Paragraph(title, h2_style))
        for item in items:
            story.append(Paragraph(f"• {item}", body_style))
        story.append(Spacer(1, 0.2*cm))

    # 适用场景
    story.append(Paragraph("适用场景", h1_style))
    scenarios = [
        "企业内部知识管理 - 构建企业知识资产",
        "技术文档中心 - 搭建开发者门户",
        "客户服务支持 - 建立客服知识库",
        "培训与学习 - 创建在线学习平台"
    ]
    for scenario in scenarios:
        story.append(Paragraph(f"• {scenario}", body_style))
    story.append(Spacer(1, 0.3*cm))

    # 客户价值
    story.append(Paragraph("客户价值", h1_style))
    values = [
        "提升效率：减少 60% 的重复问题咨询时间",
        "降低成本：节省 40% 的知识查找和整理成本",
        "促进协作：打破信息孤岛，提升团队协作效率",
        "加速创新：基于知识积累，快速复用和创新"
    ]
    for value in values:
        story.append(Paragraph(f"• {value}", body_style))

    # 生成PDF
    doc.build(story)
    print(f"[OK] PDF已生成：{pdf_file}")

def generate_excel():
    """生成价格表Excel"""
    excel_file = "docs/知识库产品价格表.xlsx"

    # 创建工作簿
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "产品价格对比"

    # 定义样式
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=12)
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    subheader_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    subheader_font = Font(bold=True, size=11)

    price_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    price_font = Font(bold=True, size=14, color="C00000")

    center_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    # 设置列宽
    ws.column_dimensions['A'].width = 25
    ws.column_dimensions['B'].width = 20
    ws.column_dimensions['C'].width = 20
    ws.column_dimensions['D'].width = 20
    ws.column_dimensions['E'].width = 20

    # 标题行
    ws.merge_cells('A1:E1')
    ws['A1'] = '知识库产品价格方案'
    ws['A1'].font = Font(bold=True, size=16, color="1F4E78")
    ws['A1'].alignment = center_alignment
    ws.row_dimensions[1].height = 30

    # 表头
    headers = ['功能特性', '免费版', '专业版', '企业版', '旗舰版']
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col)
        cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        cell.border = thin_border
    ws.row_dimensions[2].height = 25

    # 价格行
    prices = ['价格', '¥0', '¥99/用户/月', '¥299/用户/月', '按需定制']
    for col, price in enumerate(prices, start=1):
        cell = ws.cell(row=3, column=col)
        cell.value = price
        if col > 1:
            cell.fill = price_fill
            cell.font = price_font
        else:
            cell.font = Font(bold=True, size=11)
        cell.alignment = center_alignment
        cell.border = thin_border
    ws.row_dimensions[3].height = 30

    # 数据行
    data = [
        ['用户数', '最多 5 人', '20-100 人', '100-500 人', '不限'],
        ['存储空间', '5 GB', '100 GB/用户', '500 GB/用户', '不限'],
        ['文档数量', '最多 100 篇', '不限', '不限', '不限'],
        ['知识库数', '1 个', '10 个', '不限', '不限'],
        ['', '', '', '', ''],  # 空行
        ['核心功能', '', '', '', ''],
        ['全文搜索', '✓', '✓', '✓', '✓'],
        ['语义搜索', '✗', '✓', '✓', '✓'],
        ['AI 智能问答', '✗', '1000次/月', '不限', '不限'],
        ['实时协作', '✗', '✓', '✓', '✓'],
        ['自动摘要', '✗', '✓', '✓', '✓'],
        ['知识图谱', '✗', '✗', '✓', '✓'],
        ['', '', '', '', ''],  # 空行
        ['集成与API', '', '', '', ''],
        ['REST API', '✗', '10000次/天', '不限', '不限'],
        ['Webhook', '✗', '✓', '✓', '✓'],
        ['SSO单点登录', '✗', '✓', '✓', '✓'],
        ['第三方集成', '✗', '✓', '✓', '✓'],
        ['', '', '', '', ''],  # 空行
        ['安全与权限', '', '', '', ''],
        ['基础权限', '✓', '✓', '✓', '✓'],
        ['高级权限', '✗', '✓', '✓', '✓'],
        ['数据加密', '✗', '✗', '✓', '✓'],
        ['审计日志', '✗', '✗', '✓', '✓'],
        ['双因素认证', '✗', '✗', '✓', '✓'],
        ['', '', '', '', ''],  # 空行
        ['服务支持', '', '', '', ''],
        ['支持方式', '社区', '邮件/工单', '7×24支持', '专属服务'],
        ['SLA保证', '无', '99.5%', '99.9%', '99.99%'],
        ['客户经理', '✗', '✗', '✓', '✓'],
        ['培训服务', '✗', '✗', '✓', '✓'],
        ['', '', '', '', ''],  # 空行
        ['部署方式', '', '', '', ''],
        ['SaaS云服务', '✓', '✓', '✓', '✓'],
        ['私有化部署', '✗', '✗', '✗', '✓'],
        ['混合云部署', '✗', '✗', '✗', '✓'],
    ]

    current_row = 4
    for row_data in data:
        for col, value in enumerate(row_data, start=1):
            cell = ws.cell(row=current_row, column=col)
            cell.value = value
            cell.border = thin_border

            # 分类标题行
            if col == 1 and value and not any(c in value for c in ['✓', '✗', '次', 'GB', '人', '个', '篇', '%']):
                if value in ['核心功能', '集成与API', '安全与权限', '服务支持', '部署方式']:
                    cell.fill = subheader_fill
                    cell.font = subheader_font
                    cell.alignment = left_alignment
                else:
                    cell.alignment = left_alignment
            else:
                cell.alignment = center_alignment

            # 空行
            if not value:
                ws.row_dimensions[current_row].height = 8

        current_row += 1

    # 创建第二个工作表：详细价格
    ws2 = wb.create_sheet("详细价格")
    ws2.column_dimensions['A'].width = 20
    ws2.column_dimensions['B'].width = 50

    # 详细价格数据
    detailed_pricing = [
        ['版本', '价格详情'],
        ['免费版', '永久免费，适合5人以下小团队'],
        ['专业版 - 月付', '¥99/用户/月'],
        ['专业版 - 年付', '¥990/用户/年（优惠17%）'],
        ['企业版 - 月付', '¥299/用户/月'],
        ['企业版 - 年付', '¥2990/用户/年（优惠17%）'],
        ['旗舰版', '按需定制，联系销售'],
        ['', ''],
        ['附加服务', '价格'],
        ['额外存储空间', '¥50/100GB/月'],
        ['AI增强包', '¥500/月（5000次问答）'],
        ['数据迁移服务', '¥5000起'],
        ['定制培训', '¥3000/天'],
        ['技术咨询服务', '¥8000/天'],
        ['', ''],
        ['批量采购折扣', ''],
        ['100+ 用户', '10% 折扣'],
        ['500+ 用户', '15% 折扣'],
        ['1000+ 用户', '20% 折扣'],
        ['教育机构', '50% 折扣'],
        ['非营利组织', '40% 折扣'],
    ]

    for row_idx, row_data in enumerate(detailed_pricing, start=1):
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws2.cell(row=row_idx, column=col_idx)
            cell.value = value
            cell.border = thin_border

            if row_idx == 1 or value in ['附加服务', '批量采购折扣']:
                cell.fill = subheader_fill
                cell.font = subheader_font

            cell.alignment = left_alignment if col_idx == 2 else center_alignment

    # 保存文件
    wb.save(excel_file)
    print(f"[OK] Excel已生成：{excel_file}")

if __name__ == "__main__":
    print("正在生成文档...")
    generate_pdf()
    generate_excel()
    print("[OK] 所有文档生成完成！")
