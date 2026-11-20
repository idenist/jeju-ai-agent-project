"""Download a price lookup record even when it could supply no usable price."""
from __future__ import annotations

import json

import streamlit as st

from services.price_data_gap import PRICE_GAP_EVIDENCE
from ui.pdf_report import render_pdf_report


def render_price_gap_downloads(result, tool, location):
    fields = ("analysis_type", "status", "source_status", "reason", "data_gap", *PRICE_GAP_EVIDENCE)
    record = {field: result[field] for field in fields if field in result}
    entries = [{"tool": tool, "result": record}]
    content = json.dumps({"format": "jeju-price-lookup-record", "version": 1, "result": record},
                         ensure_ascii=False, indent=2)
    with st.expander("가격 조회 기록"):
        try:
            pdf = render_pdf_report(entries)
        except (ValueError, OSError):
            st.caption("PDF를 만들지 못했습니다. JSON으로 조회 기록을 저장할 수 있어요.")
        else:
            st.download_button("가격 조회 기록 PDF 저장", pdf, "감귤_가격조회기록.pdf", "application/pdf",
                               key=f"price_gap_pdf_{location}", on_click="ignore")
        st.download_button("가격 조회 기록 JSON 저장", content, "감귤_가격조회기록.json", "application/json",
                           key=f"price_gap_json_{location}", on_click="ignore")
