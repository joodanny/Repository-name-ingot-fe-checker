import re
import io
import json
import base64
from difflib import get_close_matches
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st
from openpyxl import Workbook

# ── 설정 ──────────────────────────────────────────────────────────────────────
THRESHOLD = 0.09

st.set_page_config(
    page_title="잉곳 Fe 판정기",
    page_icon="🔍",
    layout="centered"
)

# ── API 키 로드 ────────────────────────────────────────────────────────────────
def get_api_key():
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        import os
        return os.environ.get("ANTHROPIC_API_KEY", "")

# ── 자동 라벨링 ───────────────────────────────────────────────────────────────
def get_next_label() -> str:
    today = datetime.now().strftime("%y%m%d")
    if "label_counter" not in st.session_state:
        st.session_state.label_counter = {}
    count = st.session_state.label_counter.get(today, 0) + 1
    st.session_state.label_counter[today] = count
    return f"{today}-{count:02d}"

# ── 기준 데이터 로드 ──────────────────────────────────────────────────────────
@st.cache_data
def load_reference_data(csv_text: str):
    import io
    df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    df.columns = df.columns.str.strip()

    batch_col = None
    for col in df.columns:
        if "batch" in col.lower() or "casting" in col.lower() or "cast" in col.lower():
            batch_col = col
            break
    if batch_col is None:
        raise ValueError(f"Batch/Cast No 컬럼 없음. 컬럼: {list(df.columns)}")

    df = df.rename(columns={batch_col: "batch_no"})
    df["batch_no"] = (
        df["batch_no"].astype(str).str.strip().str.upper()
        .str.replace("-", "", regex=False)
        .str.replace(" ", "", regex=False)
    )
    for col in ["Fe", "Si", "Cu", "Zn", "In", "Mg", "Sn", "Ti", "Cd"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["batch_no", "Fe"]).drop_duplicates(subset=["batch_no"])
    return df

# ── Claude Vision OCR ─────────────────────────────────────────────────────────
def extract_label_with_claude(image_bytes: bytes, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
                },
                {
                    "type": "text",
                    "text": """이 이미지는 알루미늄 잉곳 라벨입니다. JSON 형식으로만 답하세요.

Vedanta 라벨이면:
{"label_type": "vedanta", "batch_no": "26D02874-26", "net_weight": 0.979, "weight_unit": "MT"}

RUSAL/Allow 라벨이면:
{"label_type": "rusal", "batch_no": "072787-16", "net_weight": 1004, "weight_unit": "kg"}

- batch_no: BATCH 번호 또는 Cast No (있는 그대로)
- net_weight: N.Wt(MT) 또는 Net kg 숫자만
- 읽을 수 없으면 null
JSON만 출력하세요."""
                }
            ]
        }]
    )

    text = message.content[0].text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return {}

# ── DB 조회 ───────────────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    return str(text).upper().strip().replace(" ", "").replace("-", "")

def lookup_batch(batch_no: str, ref_df: pd.DataFrame) -> dict:
    key = normalize(batch_no)
    row = ref_df.loc[ref_df["batch_no"] == key]
    if not row.empty:
        rec = row.iloc[0]
        fe = float(rec["Fe"])
        def get_val(col):
            return float(rec[col]) if col in rec.index and pd.notna(rec[col]) else None
        return {
            "found": True, "batch_no": key, "fe": fe,
            "si": get_val("Si"), "cu": get_val("Cu"),
            "zn": get_val("Zn"), "mg": get_val("Mg"),
            "sn": get_val("Sn"), "ti": get_val("Ti"), "cd": get_val("Cd"),
            "judgement": "0.09 이상" if fe >= THRESHOLD else "0.09 미만",
            "status": "NG" if fe >= THRESHOLD else "OK",
        }
    suggestions = get_close_matches(key, ref_df["batch_no"].tolist(), n=3, cutoff=0.80)
    return {"found": False, "batch_no": key, "suggestions": suggestions}

# ── Excel 내보내기 ────────────────────────────────────────────────────────────
def make_excel_bytes(records: list) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["라벨ID", "확인시각", "라벨유형", "Batch/Cast No",
                "N.Wt/Net", "Fe", "Si", "Cu", "Zn", "판정", "상태"])
    for r in records:
        ws.append([
            r.get("라벨ID"), r.get("확인시각"), r.get("라벨유형"),
            r.get("batch_no"), r.get("N.Wt/Net"),
            r.get("Fe"), r.get("Si"), r.get("Cu"), r.get("Zn"),
            r.get("판정"), r.get("상태")
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

# ── 결과 카드 UI ──────────────────────────────────────────────────────────────
def show_result_card(label_id, result, label_type, image_name, net_weight, weight_unit):
    st.info(f"**라벨 ID: {label_id}**")

    if net_weight is not None:
        unit_label = "N.Wt (MT)" if weight_unit == "MT" else "Net (kg)"
        st.write(f"⚖️ {unit_label}: **{net_weight}**")
    else:
        st.warning("⚠️ 무게 값을 인식하지 못했습니다.")

    if not result["found"]:
        st.warning(f"⚠️ 기준표에서 찾지 못했습니다: `{result['batch_no']}`")
        if result.get("suggestions"):
            st.write("유사 번호:")
            for s in result["suggestions"]:
                st.write(f"- `{s}`")
        # 미조회도 기록
        add_record(label_id, label_type, result["batch_no"], net_weight, weight_unit, result)
        return

    fe = result["fe"]
    if fe >= THRESHOLD:
        st.error(f"## ❌ 불합격 (NG) — Fe = {fe:.4f}")
    else:
        st.success(f"## ✅ 합격 (OK) — Fe = {fe:.4f}")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Batch/Cast No", result["batch_no"])
        st.metric("Fe", f"{fe:.4f}")
    with col2:
        if result.get("si") is not None:
            st.metric("Si", f"{result['si']:.4f}")
        if result.get("cu") is not None:
            st.metric("Cu", f"{result['cu']:.4f}")
    with col3:
        if result.get("zn") is not None:
            st.metric("Zn", f"{result['zn']:.4f}")
        if result.get("mg") is not None:
            st.metric("Mg", f"{result['mg']:.4f}")

    others = [f"{l}: {result[k]:.4f}" for k, l in
              [("sn","Sn"),("ti","Ti"),("cd","Cd")] if result.get(k) is not None]
    if others:
        st.caption("기타: " + " | ".join(others))

    add_record(label_id, label_type, result["batch_no"], net_weight, weight_unit, result)

def add_record(label_id, label_type, batch_no, net_weight, weight_unit, result):
    nw = f"{net_weight} {weight_unit}" if net_weight is not None else "-"
    record = {
        "라벨ID": label_id,
        "확인시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "라벨유형": label_type,
        "batch_no": batch_no,
        "N.Wt/Net": nw,
        "Fe": result.get("fe", ""),
        "Si": result.get("si", ""),
        "Cu": result.get("cu", ""),
        "Zn": result.get("zn", ""),
        "판정": result.get("judgement", ""),
        "상태": result.get("status", "조회불가"),
    }
    if not any(r["라벨ID"] == label_id for r in st.session_state.ingot_list):
        st.session_state.ingot_list.append(record)

# ── 이미지 처리 ───────────────────────────────────────────────────────────────
def process_image(image_bytes, image_name, source, api_key, ref_df):
    with st.spinner("🔄 라벨 인식 중... (2~3초)"):
        try:
            extracted = extract_label_with_claude(image_bytes, api_key)
        except Exception as e:
            st.error(f"API 오류: {e}")
            return

    if not extracted:
        st.error("라벨 정보를 읽지 못했습니다.")
        return

    label_type = extracted.get("label_type", "unknown")
    batch_no   = extracted.get("batch_no")
    net_weight = extracted.get("net_weight")
    weight_unit = extracted.get("weight_unit", "MT")

    type_label = "Vedanta" if label_type == "vedanta" else "RUSAL/Allow"
    st.caption(f"🏷 {type_label} 라벨 인식됨")

    if batch_no:
        edited = st.text_input("인식된 번호 (수정 가능)", value=batch_no,
                               key=f"edit_{source}_{image_name}")
        label_id = get_next_label()
        result = lookup_batch(edited, ref_df)
        show_result_card(label_id, result, type_label, image_name, net_weight, weight_unit)
    else:
        st.warning("번호를 인식하지 못했습니다.")
        manual = st.text_input("직접 번호 입력", key=f"manual_{source}_{image_name}")
        if manual:
            label_id = get_next_label()
            result = lookup_batch(manual, ref_df)
            show_result_card(label_id, result, f"{type_label}(수동)", image_name,
                             net_weight, weight_unit)

# ══════════════════════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════════════════════
st.title("🔍 잉곳 Fe 판정기")
st.caption("사진 촬영 → 자동 번호 인식 → Fe 합격/불합격 판정")

# 세션 초기화
if "ingot_list" not in st.session_state:
    st.session_state.ingot_list = []
if "label_counter" not in st.session_state:
    st.session_state.label_counter = {}

# API 키 확인
api_key = get_api_key()
if not api_key:
    st.error("⚠️ ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    st.stop()

# 기준 CSV 로드
CSV_PATH = Path(__file__).parent / "fe_reference.csv"
try:
    ref_df = load_reference_data(CSV_PATH.read_text(encoding="utf-8-sig"))
except Exception as e:
    st.error(f"❌ 기준 CSV 로드 실패: {e}")
    st.stop()

# 탭
tab_cam, tab_file, tab_manual = st.tabs(["📷 카메라 촬영", "📁 파일 업로드", "⌨️ 직접 입력"])

with tab_cam:
    st.info("📱 카메라 탭에서 바로 사진을 찍으세요!")
    cam_image = st.camera_input("라벨을 중앙에 맞추고 촬영")
    if cam_image:
        process_image(cam_image.getvalue(), cam_image.name, "카메라", api_key, ref_df)

with tab_file:
    uploaded = st.file_uploader("사진 선택", type=["jpg","jpeg","png"])
    if uploaded:
        st.image(uploaded, use_container_width=True)
        process_image(uploaded.getvalue(), uploaded.name, "파일", api_key, ref_df)

with tab_manual:
    manual_no = st.text_input("Batch/Cast No 입력 (예: 26D02823-07)")
    manual_wt = st.text_input("N.Wt 또는 Net kg (선택)", placeholder="예: 0.979 또는 1004")
    if manual_no:
        label_id = get_next_label()
        result = lookup_batch(manual_no, ref_df)
        wt_val = float(manual_wt) if manual_wt else None
        unit = "kg" if (wt_val and wt_val > 10) else "MT"
        show_result_card(label_id, result, "직접입력", "manual", wt_val, unit)

# ── 누적 목록 ─────────────────────────────────────────────────────────────────
st.divider()
st.subheader("📋 확인된 잉곳 목록")

if st.session_state.ingot_list:
    df_list = pd.DataFrame(st.session_state.ingot_list)
    st.dataframe(df_list, use_container_width=True)

    only_ng = df_list[df_list["상태"] == "NG"]
    st.write(f"총 **{len(df_list)}**건 | NG: **{len(only_ng)}**건")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🗑️ 목록 삭제"):
            st.session_state.ingot_list = []
            st.rerun()
    with col2:
        st.download_button("⬇️ 전체 엑셀",
            data=make_excel_bytes(st.session_state.ingot_list),
            file_name=f"ingot_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with col3:
        ng_list = [r for r in st.session_state.ingot_list if r.get("상태") == "NG"]
        if ng_list:
            st.download_button("⬇️ NG만 엑셀",
                data=make_excel_bytes(ng_list),
                file_name=f"ingot_NG_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # 사이드바
    with st.sidebar:
        st.header("📊 오늘 현황")
        today = datetime.now().strftime("%y%m%d")
        today_count = st.session_state.label_counter.get(today, 0)
        st.metric("오늘 처리", f"{today_count}건")
        st.caption(f"다음 라벨: {today}-{today_count+1:02d}")
else:
    st.info("아직 확인된 잉곳이 없습니다.")
