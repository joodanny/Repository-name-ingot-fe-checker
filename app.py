import re
import io
import json
import base64
import zipfile
from difflib import get_close_matches
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps
from openpyxl import Workbook

# ── 설정 ──────────────────────────────────────────────────────────────────────
THRESHOLD = 0.09
DATA_FILE = Path(__file__).parent / "ingot_data.json"

st.set_page_config(
    page_title="잉곳 Fe 판정기",
    page_icon="🔍",
    layout="centered"
)

# ── 모바일 최적화 CSS ─────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stCameraInput"] video {
    width: 100% !important;
    aspect-ratio: 16/9 !important;
    object-fit: cover !important;
    border-radius: 8px;
}
[data-testid="stCameraInput"] > div:first-child {
    aspect-ratio: 16/9 !important;
    overflow: hidden;
}
.block-container { padding: 0.5rem 1rem !important; }
[data-testid="stMetric"] label { font-size: 0.65rem !important; }
[data-testid="stMetricValue"] > div { font-size: 1.0rem !important; }
</style>
""", unsafe_allow_html=True)

# ── 후면 카메라 패치 ──────────────────────────────────────────────────────────
components.html("""
<script>
(function() {
  try {
    var md = window.parent.navigator.mediaDevices;
    if (!md || md.__patched) return;
    var orig = md.getUserMedia.bind(md);
    md.getUserMedia = function(c) {
      if (c && c.video) {
        var v = (typeof c.video === 'object') ? Object.assign({}, c.video) : {};
        v.facingMode = { ideal: 'environment' };
        c = Object.assign({}, c, { video: v });
      }
      return orig(c);
    };
    md.__patched = true;
  } catch(e) {}
})();
</script>
""", height=0)

# ── API 키 ────────────────────────────────────────────────────────────────────
def get_api_key():
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        import os
        return os.environ.get("ANTHROPIC_API_KEY", "")

# ── 데이터 파일 저장/불러오기 ─────────────────────────────────────────────────
def save_data():
    try:
        records_to_save = [{k: v for k, v in r.items() if k != "_img"}
                           for r in st.session_state.ingot_list]
        DATA_FILE.write_text(
            json.dumps({"ingot_list": records_to_save,
                        "label_counter": st.session_state.label_counter},
                       ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass

def load_data():
    try:
        if DATA_FILE.exists():
            raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if not st.session_state.ingot_list:
                saved = raw.get("ingot_list", [])
                for r in saved:
                    r.setdefault("_img", "")
                st.session_state.ingot_list = saved
            if not st.session_state.label_counter:
                st.session_state.label_counter = raw.get("label_counter", {})
    except Exception:
        pass

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
    df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    df.columns = df.columns.str.strip()
    batch_col = None
    for col in df.columns:
        if any(k in col.lower() for k in ["batch", "casting", "cast"]):
            batch_col = col
            break
    if batch_col is None:
        raise ValueError(f"Batch/Cast No 컬럼 없음. 컬럼: {list(df.columns)}")
    df = df.rename(columns={batch_col: "batch_no"})
    df["batch_no"] = (
        df["batch_no"].astype(str).str.strip().str.upper()
        .str.replace("-", "", regex=False).str.replace(" ", "", regex=False)
    )
    for col in ["Fe","Si","Cu","Zn","In","Mg","Sn","Ti","Cd"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["batch_no","Fe"]).drop_duplicates(subset=["batch_no"])

# ── 이미지 전처리 ─────────────────────────────────────────────────────────────
def preprocess_image(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()

def rotate_image_bytes(image_bytes: bytes, angle: int) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.rotate(angle, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()

# ── Claude Vision OCR ─────────────────────────────────────────────────────────
def call_claude(image_bytes: bytes, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text",
                 "text": """알루미늄 잉곳 라벨입니다. 라벨이 뒤집혀 있거나 회전되어 있어도 읽어주세요.
JSON 형식으로만 답하세요.

Vedanta 라벨:
{"label_type": "vedanta", "batch_no": "26D02874-26", "net_weight": 0.979, "weight_unit": "MT", "barcode": "바코드값", "qr_code": null}

RUSAL/Allow 라벨:
{"label_type": "rusal", "batch_no": "072787-16", "net_weight": 1004, "weight_unit": "kg", "barcode": "바코드값", "qr_code": "QR값"}

- batch_no: BATCH 번호 또는 Cast No (원본 그대로)
- net_weight: N.Wt(MT) 또는 Net kg 숫자만
- barcode: 바코드에서 읽은 텍스트 (없으면 null)
- qr_code: QR코드에서 읽은 텍스트 (없으면 null)
- 읽을 수 없으면 null
JSON만 출력하세요."""}
            ]
        }]
    )
    text = msg.content[0].text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group()) if m else {}

def extract_label(image_bytes: bytes, api_key: str) -> dict:
    for angle in [0, 180, 90, 270]:
        rotated = rotate_image_bytes(image_bytes, angle) if angle != 0 else image_bytes
        result = call_claude(rotated, api_key)
        if result.get("batch_no"):
            result["_rotated"] = angle
            return result
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

# ── Excel / ZIP ───────────────────────────────────────────────────────────────
def make_excel_bytes(records: list) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["라벨ID","확인시각","라벨유형","Batch/Cast No",
                "N.Wt/Net","Fe","Si","Cu","Zn","판정","상태","바코드","QR코드"])
    for r in records:
        ws.append([r.get(c) for c in
                   ["라벨ID","확인시각","라벨유형","batch_no",
                    "N.Wt/Net","Fe","Si","Cu","Zn","판정","상태","바코드","QR코드"]])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

def make_zip_bytes(records: list) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ingot_records.xlsx", make_excel_bytes(records))
        for r in records:
            if r.get("_img"):
                zf.writestr(f"photos/{r['라벨ID']}.jpg",
                            base64.b64decode(r["_img"]))
    buf.seek(0); return buf.getvalue()

# ── 인식 결과 카드 ─────────────────────────────────────────────────────────────
def show_recognition_result(pending: dict, ref_df: pd.DataFrame):
    extracted   = pending["extracted"]
    image_bytes = pending["image_bytes"]

    batch_no    = extracted.get("batch_no", "")
    net_weight  = extracted.get("net_weight")
    weight_unit = extracted.get("weight_unit", "MT")
    label_type  = "Vedanta" if extracted.get("label_type") == "vedanta" else "RUSAL/Allow"
    rotated     = extracted.get("_rotated", 0)
    barcode     = extracted.get("barcode")
    qr_code     = extracted.get("qr_code")

    st.image(image_bytes, use_container_width=True)
    if rotated:
        st.caption(f"🔄 {rotated}° 회전하여 인식")

    edited_no = st.text_input("인식된 번호 (수정 가능)", value=batch_no,
                              key="pending_batch_edit")

    unit_label = "N.Wt (MT)" if weight_unit == "MT" else "Net (kg)"
    edited_wt  = st.text_input(f"⚖️ {unit_label} (수정 가능)",
                               value=str(net_weight) if net_weight is not None else "",
                               key="pending_nw_edit")

    result = lookup_batch(edited_no, ref_df) if edited_no else None

    if result:
        if result["found"]:
            fe = result["fe"]
            if fe >= THRESHOLD:
                st.error(f"## ❌ 불합격 (NG) — Fe = {fe:.4f}")
            else:
                st.success(f"## ✅ 합격 (OK) — Fe = {fe:.4f}")
        else:
            st.warning(f"⚠️ 기준표에 없는 번호: `{result['batch_no']}`")
            if result.get("suggestions"):
                st.write("유사 번호: " + " / ".join(f"`{s}`" for s in result["suggestions"]))

    col_ok, col_retry = st.columns(2)
    with col_ok:
        if st.button("✅ 확인 (리스트 추가)", use_container_width=True,
                     disabled=not (result and edited_no), type="primary"):
            if result:
                label_id = get_next_label()
                try:
                    wt_val = float(edited_wt) if edited_wt.strip() else None
                except ValueError:
                    wt_val = None
                nw = str(wt_val) if wt_val is not None else "-"
                img_b64 = base64.b64encode(image_bytes).decode()
                st.session_state.ingot_list.append({
                    "라벨ID":   label_id,
                    "확인시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "라벨유형": label_type,
                    "batch_no": result.get("batch_no", edited_no),
                    "N.Wt/Net": nw,
                    "Fe":  result.get("fe", ""),
                    "Si":  result.get("si", ""),
                    "Cu":  result.get("cu", ""),
                    "Zn":  result.get("zn", ""),
                    "판정": result.get("judgement", ""),
                    "상태": result.get("status", "조회불가"),
                    "바코드": barcode or "",
                    "QR코드": qr_code or "",
                    "_img": img_b64,
                })
                save_data()
                st.success(f"✅ {label_id} 저장 완료!")
                st.session_state.pending = None
                st.session_state.cam_key += 1
                st.rerun()
    with col_retry:
        if st.button("🔄 다시 찍기", use_container_width=True):
            st.session_state.pending = None
            st.session_state.cam_key += 1
            st.rerun()

    if result and result["found"]:
        st.divider()
        col1, col2, col3 = st.columns(3)
        fe = result["fe"]
        with col1:
            st.metric("Batch/Cast No", result["batch_no"])
            st.metric("Fe", f"{fe:.4f}")
        with col2:
            if result.get("si"): st.metric("Si", f"{result['si']:.4f}")
            if result.get("cu"): st.metric("Cu", f"{result['cu']:.4f}")
        with col3:
            if result.get("zn"): st.metric("Zn", f"{result['zn']:.4f}")
            if result.get("mg"): st.metric("Mg", f"{result['mg']:.4f}")

    if barcode or qr_code:
        st.divider()
        if barcode: st.caption(f"📊 바코드: `{barcode}`")
        if qr_code: st.caption(f"📱 QR코드: `{qr_code}`")

# ══════════════════════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════════════════════
st.title("🔍 잉곳 Fe 판정기")
st.caption("사진 촬영 → 번호 인식 → 확인 버튼으로 저장")

for key, val in [("ingot_list",[]), ("label_counter",{}),
                 ("pending", None), ("cam_key", 0)]:
    if key not in st.session_state:
        st.session_state[key] = val

load_data()

api_key = get_api_key()
if not api_key:
    st.error("⚠️ ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    st.stop()

CSV_PATH = Path(__file__).parent / "fe_reference.csv"
try:
    ref_df = load_reference_data(CSV_PATH.read_text(encoding="utf-8-sig"))
except Exception as e:
    st.error(f"❌ 기준 CSV 로드 실패: {e}"); st.stop()

# ── 탭 ────────────────────────────────────────────────────────────────────────
tab_cam, tab_file, tab_manual, tab_barcode = st.tabs(
    ["📷 카메라 촬영", "📁 파일 업로드", "⌨️ 직접 입력", "📊 바코드 스캔"]
)

# ── 카메라 탭 ─────────────────────────────────────────────────────────────────
with tab_cam:
    if st.session_state.pending:
        show_recognition_result(st.session_state.pending, ref_df)
    else:
        st.info("📱 라벨을 가로로 맞추고 촬영하세요! (후면 카메라가 자동 선택됩니다)")
        cam_image = st.camera_input("라벨을 중앙에 맞추고 촬영",
                                    key=f"cam_{st.session_state.cam_key}")
        if cam_image:
            processed = preprocess_image(cam_image.getvalue())
            with st.spinner("🔄 라벨 인식 중... (2~3초)"):
                try:
                    extracted = extract_label(processed, api_key)
                except Exception as e:
                    st.error(f"API 오류: {e}"); extracted = {}

            if extracted.get("batch_no"):
                st.session_state.pending = {
                    "extracted":   extracted,
                    "image_bytes": processed,
                    "source":      "카메라",
                }
                st.rerun()
            else:
                st.warning("번호를 인식하지 못했습니다. 더 가까이, 선명하게 찍어보세요.")
                if st.button("🔄 다시 찍기", key="retry_cam"):
                    st.session_state.cam_key += 1
                    st.rerun()

# ── 파일 업로드 탭 ────────────────────────────────────────────────────────────
with tab_file:
    uploaded = st.file_uploader("사진 선택", type=["jpg","jpeg","png"])
    if uploaded:
        processed = preprocess_image(uploaded.getvalue())
        st.image(processed, use_container_width=True)
        with st.spinner("🔄 라벨 인식 중..."):
            try:
                extracted = extract_label(processed, api_key)
            except Exception as e:
                st.error(f"API 오류: {e}"); extracted = {}

        if extracted.get("batch_no"):
            st.session_state.pending = {
                "extracted":   extracted,
                "image_bytes": processed,
                "source":      "파일",
            }
            show_recognition_result(st.session_state.pending, ref_df)
        else:
            st.warning("번호를 인식하지 못했습니다.")

# ── 직접 입력 탭 ──────────────────────────────────────────────────────────────
with tab_manual:
    manual_no = st.text_input("Batch/Cast No 입력 (예: 26D02823-07)")
    manual_wt = st.text_input("N.Wt 또는 Net kg (선택)", placeholder="예: 0.979 또는 1004")
    if manual_no and st.button("🔍 조회 및 추가", type="primary"):
        result = lookup_batch(manual_no, ref_df)
        wt_val = float(manual_wt) if manual_wt else None
        label_id = get_next_label()
        nw = str(wt_val) if wt_val else "-"
        st.session_state.ingot_list.append({
            "라벨ID":   label_id,
            "확인시각": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "라벨유형": "직접입력",
            "batch_no": result.get("batch_no", normalize(manual_no)),
            "N.Wt/Net": nw,
            "Fe":  result.get("fe",""), "Si": result.get("si",""),
            "Cu":  result.get("cu",""), "Zn": result.get("zn",""),
            "판정": result.get("judgement",""),
            "상태": result.get("status","조회불가"),
            "바코드": "", "QR코드": "",
            "_img": "",
        })
        save_data()
        if result["found"]:
            fe = result["fe"]
            if fe >= THRESHOLD:
                st.error(f"❌ 불합격 (NG) — Fe = {fe:.4f} → {label_id} 저장됨")
            else:
                st.success(f"✅ 합격 (OK) — Fe = {fe:.4f} → {label_id} 저장됨")
        else:
            st.warning(f"기준표에 없음 → {label_id} 저장됨 (Fe 미기재)")

# ── 바코드 스캔 탭 ────────────────────────────────────────────────────────────
with tab_barcode:
    st.info("📷 바코드/QR코드를 가까이, 선명하게 촬영하세요.")

    if "barcode_cam_key" not in st.session_state:
        st.session_state.barcode_cam_key = 0

    barcode_photo = st.camera_input("바코드/QR코드 촬영",
                                    key=f"bc_cam_{st.session_state.barcode_cam_key}")

    # 인식 결과 표시 및 편집
    scanned_default = ""
    if barcode_photo:
        bc_img = preprocess_image(barcode_photo.getvalue())
        with st.spinner("🔄 바코드 인식 중..."):
            try:
                from pyzbar import pyzbar as pzb
                from PIL import ImageEnhance, ImageFilter

                base = Image.open(io.BytesIO(bc_img)).convert("RGB")
                gray = base.convert("L")

                # 순서대로 시도 — 성공하면 즉시 반환
                def try_all(img_list):
                    for im in img_list:
                        r = pzb.decode(im)
                        if r:
                            return r
                    return []

                w, h = base.size
                attempts = [
                    base,                                                      # 원본 RGB
                    gray,                                                      # 그레이스케일
                    ImageEnhance.Contrast(gray).enhance(2.0),                 # 대비 2배
                    ImageEnhance.Contrast(gray).enhance(3.5),                 # 대비 3.5배
                    ImageEnhance.Sharpness(gray).enhance(3.0),                # 선명도
                    base.filter(ImageFilter.SHARPEN).convert("L"),            # 샤프닝
                    base.resize((w*2, h*2), Image.LANCZOS),                   # 2배 확대
                    base.resize((w*2, h*2), Image.LANCZOS).convert("L"),      # 2배+그레이
                    ImageEnhance.Contrast(
                        base.resize((w*2, h*2), Image.LANCZOS).convert("L")
                    ).enhance(3.0),                                            # 2배+대비
                    base.resize((w*3, h*3), Image.LANCZOS).convert("L"),      # 3배 확대
                ]

                decoded = try_all(attempts)

                if decoded:
                    scanned_default = decoded[0].data.decode("utf-8", errors="replace")
                    types = [d.type for d in decoded]
                    # session_state에 직접 주입 → text_input에 자동 반영
                    st.session_state["scanned_barcode"] = scanned_default
                    st.success(f"✅ 인식됨 ({', '.join(types)}): `{scanned_default}`")
                else:
                    st.warning(
                        "❌ 인식 실패\n\n"
                        "**촬영 방법:**\n"
                        "- 바코드가 화면을 **꽉 채우도록** 가까이 찍기\n"
                        "- 바코드를 **수평으로** 맞추기 (기울이지 않기)\n"
                        "- 그림자 없이 **밝은 곳**에서 찍기\n"
                        "- 핸드폰을 **고정**하고 흔들리지 않게 찍기"
                    )
            except ImportError as e:
                st.error(f"⚠️ pyzbar 로드 실패: {e}")
            except Exception as e:
                st.error(f"오류: {type(e).__name__}: {e}")

    scanned = st.text_input(
        "📊 바코드 인식 결과 (수정 가능)",
        value=scanned_default,
        key="scanned_barcode",
        placeholder="촬영 후 자동 입력됩니다"
    )

    if st.button("🔄 다시 찍기", key="barcode_retry"):
        st.session_state.barcode_cam_key += 1
        st.rerun()

    if st.session_state.ingot_list:
        recent  = list(reversed(st.session_state.ingot_list[-5:]))
        options = [
            f"{r['라벨ID']}  |  {r['batch_no']}  |  바코드: {r.get('바코드') or '없음'}"
            for r in recent
        ]
        sel = st.radio("저장할 잉곳 선택", range(len(options)),
                       format_func=lambda i: options[i],
                       key="barcode_target")
        target = recent[sel]

        if st.button("💾 바코드 저장", type="primary", disabled=not scanned):
            for r in st.session_state.ingot_list:
                if r["라벨ID"] == target["라벨ID"]:
                    r["바코드"] = scanned
                    break
            save_data()
            st.success(f"✅ [{target['라벨ID']}] 바코드 저장 완료: `{scanned}`")
            # 카메라 초기화 → 촬영 모드로 복귀
            st.session_state.barcode_cam_key += 1
            st.session_state["scanned_barcode"] = ""
            st.rerun()
    else:
        st.warning("⚠️ 먼저 📷 카메라 탭에서 잉곳을 추가하세요.")

# ── 누적 목록 ─────────────────────────────────────────────────────────────────
st.divider()
st.subheader("📋 확인된 잉곳 목록")

if st.session_state.ingot_list:
    display_cols = ["라벨ID","확인시각","라벨유형","batch_no",
                    "N.Wt/Net","Fe","Si","Cu","Zn","판정","상태","바코드","QR코드"]
    df_raw = pd.DataFrame(st.session_state.ingot_list)
    for c in display_cols:
        if c not in df_raw.columns:
            df_raw[c] = ""
    st.dataframe(df_raw[display_cols], use_container_width=True)

    only_ng = [r for r in st.session_state.ingot_list if r.get("상태") == "NG"]
    st.write(f"총 **{len(st.session_state.ingot_list)}**건 | NG: **{len(only_ng)}**건")

    photos = [(r["라벨ID"], r["_img"])
              for r in st.session_state.ingot_list if r.get("_img")]
    if photos:
        with st.expander(f"📸 촬영 사진 ({len(photos)}장)"):
            cols = st.columns(3)
            for i, (lid, b64) in enumerate(photos):
                with cols[i % 3]:
                    st.image(base64.b64decode(b64), caption=lid,
                             use_container_width=True)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        if st.button("🗑️ 초기화"):
            st.session_state.ingot_list = []
            st.session_state.label_counter = {}
            try:
                DATA_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            st.rerun()
    with c2:
        st.download_button("⬇️ 전체 엑셀",
            data=make_excel_bytes(st.session_state.ingot_list),
            file_name=f"ingot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with c3:
        if only_ng:
            st.download_button("⬇️ NG 엑셀",
                data=make_excel_bytes(only_ng),
                file_name=f"ingot_NG_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with c4:
        st.download_button("⬇️ 사진+엑셀 ZIP",
            data=make_zip_bytes(st.session_state.ingot_list),
            file_name=f"ingot_zip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip")

    with st.sidebar:
        st.header("📊 오늘 현황")
        today = datetime.now().strftime("%y%m%d")
        today_count = st.session_state.label_counter.get(today, 0)
        st.metric("오늘 처리", f"{today_count}건")
        st.caption(f"다음 라벨: {today}-{today_count+1:02d}")
else:
    st.info("아직 확인된 잉곳이 없습니다.")
