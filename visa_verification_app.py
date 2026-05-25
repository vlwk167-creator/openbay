import streamlit as st
import requests
import json
import time
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import anthropic
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import io

st.set_page_config(page_title="Visa Offer Verification", page_icon="x1F4B3", layout="wide")
st.title("Visa Marketing Offer Verification")
st.caption("Google Sheets의 머천트 데이터를 Visa API와 Claude AI로 자동 검증합니다.")

def load_from_secrets():
    try:
        claude_key = st.secrets["ANTHROPIC_API_KEY"]
        sa_info = dict(st.secrets["gcp_service_account"])
        gmail_user = st.secrets.get("GMAIL_USER", None)
        gmail_pw   = st.secrets.get("GMAIL_APP_PASSWORD", None)
        return claude_key, json.dumps(sa_info), gmail_user, gmail_pw
    except Exception:
        return None, None, None, None

_claude_key, _sa_json, _gmail_user, _gmail_pw = load_from_secrets()
_secrets_loaded = _claude_key is not None

with st.sidebar:
    st.header("설정")
    st.subheader("Google Sheets")
    spreadsheet_id = st.text_input("Spreadsheet ID", value="1_RiUa5TYgw60Y2wDjrif40HUsalGTv_6ZdNweDDLhqo")
    sheet_name = st.text_input("시트 이름", value="시트1")
    if _secrets_loaded:
        st.success("Service Account & API Key 자동 로드됨", icon="🔐")
        service_account_json = _sa_json
        claude_api_key = _claude_key
    else:
        st.subheader("Google Service Account")
        service_account_json = st.text_area("Service Account JSON", height=120, placeholder='{"type": "service_account", ...}')
        st.subheader("Claude API")
        claude_api_key = st.text_input("Claude API Key", type="password")
    st.subheader("Gmail 이메일 발송")
    recipient_email = st.text_input("수신자 이메일", placeholder="example@gmail.com")
    if _gmail_user and _gmail_pw:
        st.success("Gmail 계정 자동 로드됨", icon="📧")
        gmail_sender = _gmail_user
        gmail_app_pw = _gmail_pw
    else:
        gmail_sender = st.text_input("발신 Gmail 주소", placeholder="yourname@gmail.com")
        gmail_app_pw = st.text_input("Gmail 앱 비밀번호", type="password", help="Google 계정 > 보안 > 앱 비밀번호에서 생성")
    st.subheader("Visa API")
    visa_locale = st.selectbox("Locale", ["in_id", "th_th", "ko_kr", "ja_jp", "zh_hk"], index=0)
    api_delay = st.slider("API 호출 간격 (초)", min_value=1, max_value=10, value=3)
    run_btn = st.button("검증 시작", type="primary", use_container_width=True)

@st.cache_data(ttl=60, show_spinner=False)
def fetch_sheet_data(spreadsheet_id, sheet_name, sa_json):
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]
    creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(spreadsheet_id).worksheet(sheet_name)
    return pd.DataFrame(sheet.get_all_records())

def fetch_visa_offer(offer_id, locale):
    url = f"https://www.visa.co.id/offers/api/offer/{offer_id}?locale={locale}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

def verify_with_claude(merchant_name, location, offer, campaign_end, api_data, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "너는 다국어 데이터 검증 전문가야. [시트]와 [API 원본 데이터]를 대조해.\n"
        "반드시 아래 JSON 포맷으로만 답변하고, 다른 말은 절대 하지 마.\n\n"
        "### 검증 로직\n"
        "1. 페이지 부재 (no_page): API 결과 텍스트가 비어있거나, 머천트 이름이 시트와 전혀 관련 없다면 무조건 status: no_page.\n"
        "2. 정상 (checked + true): API 전체 데이터를 샅샅이 뒤져서, 시트의 조건과 문맥상 모두 일치하면 status: checked 및 모든 match 필드를 true로 설정.\n"
        "   * 주의사항: 오퍼 검증 시 오퍼 요약뿐만 아니라 오퍼 상세(Perincian 등)와 이용 약관까지 꼼꼼히 찾아 읽고 판별해.\n"
        "3. 틀림 (checked + false): 데이터는 있으나 날짜, 오퍼 조건, 로케이션 중 하나라도 다르면 status: checked 및 해당 match 필드를 false로 설정.\n\n"
        f"[시트 데이터]\n"
        f"- 타겟: {merchant_name} ({location})\n"
        f"- 로케이션: {location}\n"
        f"- 오퍼: {offer}\n"
        f"- 종료일: {campaign_end}\n\n"
        f"[API 원본 데이터]\n{json.dumps(api_data, ensure_ascii=False)}\n\n"
        "[출력 JSON]\n"
        "{\n"
        f'  "merchant_full_name": "{merchant_name}({location})",\n'
        '  "status": "checked" 또는 "no_page",\n'
        '  "location_match": true/false,\n'
        '  "date_match": true/false,\n'
        '  "offer_match": true/false,\n'
        '  "reason": "불일치 시 상세 사유 작성"\n'
        "}"
    )
    message = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, messages=[{"role": "user", "content": prompt}])
    raw = message.content[0].text.strip()
    return json.loads(raw[raw.find("{"):raw.rfind("}")+1])

def send_result_email(sender, app_password, recipient, results_by_category, result_df):
    total     = sum(len(v) for v in results_by_category.values())
    n_ok      = len(results_by_category["success"])
    n_fail    = len(results_by_category["failed"])
    n_nopage  = len(results_by_category["no_page"])
    n_missing = len(results_by_category["missing"])

    detail_rows = ""
    for item in results_by_category["failed"] + results_by_category["no_page"]:
        name   = item.get("merchant_full_name", "")
        cat    = "불일치" if item.get("category") == "failed" else "페이지 없음"
        loc    = "O" if str(item.get("location_match", "")).lower() == "true" else ("X" if item.get("location_match") is not None else "-")
        date_v = "O" if str(item.get("date_match", "")).lower() == "true" else ("X" if item.get("date_match") is not None else "-")
        offer_v= "O" if str(item.get("offer_match", "")).lower() == "true" else ("X" if item.get("offer_match") is not None else "-")
        reason = item.get("reason", "")
        color  = "#E65100" if cat == "불일치" else "#C62828"
        detail_rows += (
            "<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{name}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:{color};font-weight:600;'>{cat}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;'>{loc}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;'>{date_v}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;'>{offer_v}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-size:12px;color:#555;'>{reason}</td>"
            "</tr>"
        )

    if detail_rows:
        detail_table = (
            "<table style='width:100%;border-collapse:collapse;margin-top:16px;font-size:13px;'>"
            "<thead><tr style='background:#f5f5f5;'>"
            "<th style='padding:8px 10px;text-align:left;'>머천트</th>"
            "<th style='padding:8px 10px;text-align:left;'>결과</th>"
            "<th style='padding:8px 10px;text-align:center;'>위치</th>"
            "<th style='padding:8px 10px;text-align:center;'>날짜</th>"
            "<th style='padding:8px 10px;text-align:center;'>오퍼</th>"
            "<th style='padding:8px 10px;text-align:left;'>사유</th>"
            "</tr></thead>"
            f"<tbody>{detail_rows}</tbody>"
            "</table>"
        )
    else:
        detail_table = "<p style='color:#555;'>불일치/페이지 없음 항목이 없습니다.</p>"

    html_body = (
        "<html><body style='font-family:sans-serif;color:#222;max-width:800px;margin:auto;'>"
        "<h2 style='color:#1a237e;'>Visa 오퍼 검증 결과 보고서</h2>"
        "<p>검증이 완료되었습니다. 아래는 결과 요약입니다.</p>"
        "<table style='border-collapse:collapse;margin-bottom:24px;'><tr>"
        f"<td style='padding:12px 24px;background:#2E7D32;color:#fff;border-radius:6px;text-align:center;'>"
        f"<div style='font-size:28px;font-weight:700;'>{n_ok}</div><div>정상</div></td>"
        "<td style='width:8px;'></td>"
        f"<td style='padding:12px 24px;background:#E65100;color:#fff;border-radius:6px;text-align:center;'>"
        f"<div style='font-size:28px;font-weight:700;'>{n_fail}</div><div>불일치</div></td>"
        "<td style='width:8px;'></td>"
        f"<td style='padding:12px 24px;background:#C62828;color:#fff;border-radius:6px;text-align:center;'>"
        f"<div style='font-size:28px;font-weight:700;'>{n_nopage}</div><div>페이지 없음</div></td>"
        "<td style='width:8px;'></td>"
        f"<td style='padding:12px 24px;background:#546E7A;color:#fff;border-radius:6px;text-align:center;'>"
        f"<div style='font-size:28px;font-weight:700;'>{n_missing}</div><div>ID 누락</div></td>"
        "</tr></table>"
        f"<p style='color:#555;'>전체 {total}건 검증 완료</p>"
        "<h3>불일치 / 페이지 없음 상세</h3>"
        f"{detail_table}"
        "<p style='margin-top:24px;font-size:12px;color:#999;'>전체 결과 CSV가 첨부파일로 포함되어 있습니다.</p>"
        "</body></html>"
    )

    subject = f"[Visa 검증] 결과 보고 - 정상 {n_ok} / 불일치 {n_fail} / 페이지없음 {n_nopage} / 누락 {n_missing}"
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    csv_bytes = result_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    attachment = MIMEBase("application", "octet-stream")
    attachment.set_payload(csv_bytes)
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", "attachment", filename="visa_verification_result.csv")
    msg.attach(attachment)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, recipient, msg.as_string())

def classify_result(result):
    if result.get("status") == "no_page":
        return "no_page"
    if result.get("status") == "checked":
        loc   = str(result.get("location_match", "true")).lower() == "true"
        date  = str(result.get("date_match", "true")).lower() == "true"
        offer = str(result.get("offer_match", "true")).lower() == "true"
        return "success" if (loc and date and offer) else "failed"
    return "failed"

STATUS_META = {
    "success": {"label": "정상",        "color": "#2E7D32", "badge": "V 정상"},
    "failed":  {"label": "불일치",      "color": "#E65100", "badge": "X 불일치"},
    "no_page": {"label": "페이지 없음", "color": "#C62828", "badge": "X 페이지 없음"},
    "missing": {"label": "ID 누락",     "color": "#546E7A", "badge": "- ID 누락"},
}

if run_btn:
    missing_fields = []
    if not spreadsheet_id: missing_fields.append("Spreadsheet ID")
    if not service_account_json: missing_fields.append("Service Account JSON")
    if not claude_api_key: missing_fields.append("Claude API Key")
    if missing_fields:
        st.error(f"다음 항목을 입력해 주세요: {', '.join(missing_fields)}")
        st.stop()

    with st.status("Google Sheets 데이터를 읽는 중...", expanded=True) as status:
        try:
            df = fetch_sheet_data(spreadsheet_id, sheet_name, service_account_json)
            status.update(label=f"시트 로드 완료 - 총 {len(df)}건", state="complete")
        except Exception as e:
            status.update(label="시트 로드 실패", state="error")
            st.error(f"Google Sheets 연결 오류: {e}")
            st.stop()

    st.divider()
    results_by_category = {"success": [], "failed": [], "missing": [], "no_page": []}
    all_rows = []
    progress = st.progress(0, text="검증 준비 중...")
    log_area = st.empty()
    log_lines = []
    total = len(df)

    for idx, row in df.iterrows():
        merchant     = str(row.get("Merchant Name", "")).strip()
        location     = str(row.get("Location(s)", "")).strip()
        offer_text   = str(row.get("Offer", "")).strip()
        campaign_end = str(row.get("Campaign End", "")).strip()
        offer_id     = str(row.get("Offer ID", "")).strip()
        pct = int((idx + 1) / total * 100)
        progress.progress(pct, text=f"[{idx+1}/{total}] {merchant} ({location})")

        if not offer_id or offer_id in ("", "0", "nan", "None"):
            entry = {
                "merchant_full_name": f"{merchant}({location})",
                "status": "missing",
                "location_match": None,
                "date_match": None,
                "offer_match": None,
                "reason": "Offer ID가 시트에 입력되지 않았습니다.",
                "category": "missing"
            }
            results_by_category["missing"].append(entry)
            all_rows.append(entry)
            log_lines.append(f"[{idx+1}] {merchant} - Offer ID 누락, 스킵")
            log_area.code("\n".join(log_lines), language=None)
            continue

        if idx > 0:
            time.sleep(api_delay)

        log_lines.append(f"-> [{idx+1}] {merchant} - Visa API 호출 중...")
        log_area.code("\n".join(log_lines), language=None)
        api_data = fetch_visa_offer(offer_id, visa_locale)
        time.sleep(2)

        log_lines[-1] = f"-> [{idx+1}] {merchant} - Claude AI 검증 중..."
        log_area.code("\n".join(log_lines), language=None)

        try:
            ai_result = verify_with_claude(merchant, location, offer_text, campaign_end, api_data, claude_api_key)
            category = classify_result(ai_result)
            ai_result["category"] = category
            results_by_category[category].append(ai_result)
            all_rows.append(ai_result)
            meta = STATUS_META[category]
            log_lines[-1] = f"{meta['badge']}  [{idx+1}] {merchant} - {meta['label']}"
            if category == "failed":
                log_lines[-1] += f"  |  {ai_result.get('reason', '')}"
        except Exception as e:
            entry = {
                "merchant_full_name": f"{merchant}({location})",
                "status": "error",
                "reason": str(e),
                "category": "failed"
            }
            results_by_category["failed"].append(entry)
            all_rows.append(entry)
            log_lines[-1] = f"X  [{idx+1}] {merchant} - Claude 오류: {e}"
        log_area.code("\n".join(log_lines), language=None)

    progress.progress(100, text="검증 완료")
    st.divider()
    st.subheader("검증 결과 요약")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("정상", len(results_by_category["success"]))
    col2.metric("불일치", len(results_by_category["failed"]))
    col3.metric("페이지 없음", len(results_by_category["no_page"]))
    col4.metric("ID 누락", len(results_by_category["missing"]))
    st.divider()

    for cat_key, cat_label in [
        ("success", "정상"),
        ("failed", "불일치 (오퍼/날짜/위치)"),
        ("no_page", "페이지가 존재하지 않음"),
        ("missing", "Offer ID 누락")
    ]:
        items = results_by_category[cat_key]
        meta = STATUS_META[cat_key]
        with st.expander(f"{cat_label} - {len(items)}건", expanded=(len(items) > 0 and cat_key != "success")):
            if not items:
                st.caption("해당 없음")
                continue
            for item in items:
                name = item.get("merchant_full_name", "Unknown")
                reason = item.get("reason", "")
                left, right = st.columns([3, 7])
                with left:
                    st.markdown(f"<span style='color:{meta['color']};font-weight:600;'>{meta['badge']}</span>", unsafe_allow_html=True)
                    st.write(f"**{name}**")
                with right:
                    if cat_key == "failed":
                        checks = []
                        for k, label in [("location_match", "위치"), ("date_match", "날짜"), ("offer_match", "오퍼")]:
                            v = item.get(k)
                            if v is not None:
                                checks.append(f"{label}: {'O' if str(v).lower() == 'true' else 'X'}")
                        st.caption("  |  ".join(checks))
                    if reason:
                        st.caption(reason)
                st.divider()

    st.subheader("전체 결과 내보내기")
    result_df = pd.DataFrame(all_rows).reindex(
        columns=["merchant_full_name", "category", "location_match", "date_match", "offer_match", "reason"]
    )
    result_df.columns = ["머천트", "결과", "위치 일치", "날짜 일치", "오퍼 일치", "사유"]
    st.dataframe(result_df, use_container_width=True)
    st.download_button(
        "CSV 다운로드",
        data=result_df.to_csv(index=False, encoding="utf-8-sig"),
        file_name="visa_verification_result.csv",
        mime="text/csv",
        use_container_width=True
    )

    # 이메일 자동 발송
    st.divider()
    if recipient_email and gmail_sender and gmail_app_pw:
        with st.spinner(f"이메일을 {recipient_email} 으로 발송 중..."):
            try:
                send_result_email(gmail_sender, gmail_app_pw, recipient_email, results_by_category, result_df)
                st.success(f"결과 이메일을 {recipient_email} 으로 발송했습니다.", icon="x1F4EC")
            except Exception as e:
                st.error(f"이메일 발송 실패: {e}")
    elif recipient_email:
        st.warning("Gmail 발신 주소 또는 앱 비밀번호가 입력되지 않아 이메일을 발송하지 않았습니다.")
    else:
        st.info("수신자 이메일을 입력하면 검증 완료 후 자동으로 결과를 발송합니다.")

else:
    st.info("사이드바에서 검증 시작 버튼을 클릭하세요.")
