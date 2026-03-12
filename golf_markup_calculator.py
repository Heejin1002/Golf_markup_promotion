import streamlit as st
import streamlit.components.v1 as components
import re
import pandas as pd
import math
import html
import datetime

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

st.set_page_config(page_title="골프 요금 마크업 계산기", layout="wide")


def export_df_to_google_sheets(
    df: pd.DataFrame,
    spreadsheet_id: str,
    sheet_name: str = "요금표",
    product_name: str = "",
    period: str = "",
    extracted_date: str | None = None,
):
    """DataFrame을 구글 스프레드시트에 '추가'로 씁니다.

    - 기존 시트는 유지 (덮어쓰기/clear 하지 않음)
    - 헤더는 이미 있다고 가정하되, A1=상품명 / B1= / C1=기간은 필요 시 채움
    - 데이터는 마지막 행 다음 줄부터 append
    - 각 데이터 행: A=상품명, B=비움, C=기간, D열부터 요금표 데이터
    """
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread 패키지가 필요합니다. pip install gspread google-auth")
    creds_dict = st.secrets.get("gcp_service_account")
    if not creds_dict:
        raise RuntimeError("스트림릿 시크릿에 gcp_service_account를 설정해 주세요.")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows=max(100, len(df) + 10), cols=max(26, len(df.columns) + 4))

    # 헤더 보정 (A1:C1만)
    try:
        header_row = worksheet.row_values(1)
    except Exception:
        header_row = []
    if len(header_row) < 1 or (header_row[0] or "").strip() != "상품명":
        worksheet.update_acell("A1", "상품명")
    # B1은 비워둠
    if len(header_row) < 3 or (header_row[2] or "").strip() != "기간":
        worksheet.update_acell("C1", "기간")

    # 마지막 데이터 행 다음에 append
    all_vals = worksheet.get_all_values()
    next_row = len(all_vals) + 1 if all_vals else 2  # 헤더가 있으면 보통 2부터

    extracted_date = extracted_date or datetime.date.today().isoformat()
    df_str = df.astype(str)
    rows = []
    for i in range(len(df_str)):
        row = [product_name, "", period] + df_str.iloc[i].tolist()
        # W열(23번째, 1-indexed)에 '추출날짜' 값을 넣기 위해 패딩
        # 0-index 기준 W=22
        if len(row) <= 22:
            row.extend([""] * (23 - len(row)))
        row[22] = extracted_date
        rows.append(row)

    if not rows:
        return True

    # 필요 시 시트 row 수 확장
    needed_rows = next_row + len(rows) - 1
    if worksheet.row_count < needed_rows:
        worksheet.add_rows(needed_rows - worksheet.row_count)

    start_cell = f"A{next_row}"
    # gspread 최신 버전에서는 update(values 먼저)로 변경되어 named args로 전달
    worksheet.update(range_name=start_cell, values=rows, value_input_option="USER_ENTERED")
    return True


# ─────────────────────────────────────────────
#  HTML 파서: 골프 요금표
# ─────────────────────────────────────────────
def parse_golf_html(html: str):
    """
    골프 요금 HTML에서 데이터 추출 (mk 세일가만 사용).
    hole 블록은 renderRow hidden input 기준으로 분리.
    """
    rows = []

    # renderRow hidden input 기준으로 hole 블록 분리
    # 각 hole의 시작은 renderRow.{hole} input이 있는 <tr>
    hole_starts = list(re.finditer(
        r'name="golf_rate\.rateJson\.renderRow\.([^"]+)"',
        html
    ))

    for i, m in enumerate(hole_starts):
        hole = m.group(1)

        # 이 hole 블록의 범위: 현재 renderRow부터 다음 renderRow 전까지
        block_start = html.rfind('<tr', 0, m.start())  # renderRow가 속한 <tr> 시작
        block_end = hole_starts[i + 1].start() if i + 1 < len(hole_starts) else len(html)
        # 다음 hole의 <tr> 시작점으로 맞춤
        if i + 1 < len(hole_starts):
            block_end = html.rfind('<tr', 0, hole_starts[i + 1].start())
        block = html[block_start:block_end]

        # ── 캐디피 / 카트피 (hole 단위 공통)
        caddy_net = _extract_val(block, rf'name="golf_rate\.rateJson\.caddy\.{re.escape(hole)}\.nett"')
        caddy_sale_thb = _extract_val(block, rf'name="golf_rate\.rateJson\.caddy\.{re.escape(hole)}\.sale\.THB"')
        cart_net = _extract_val(block, rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(hole)}\.nett"')
        cart_sale_thb = _extract_val(block, rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(hole)}\.sale\.THB"')

        # ── 시간대별 파싱 (iveTrNett 기준)
        time_blocks = re.split(r'(?=<tr[^>]+class="[^"]*iveTrNett[^"]*")', block)
        for tb in time_blocks:
            if 'iveTrNett' not in tb:
                continue

            time_match = re.search(r'class="column-fixed-1">([^<]+)</th>', tb)
            if not time_match:
                continue
            time_of_day = time_match.group(1).strip()

            for week_div in ('weekday', 'weekend'):
                net_key = rf'name="golf_rate\.rateJson\.{week_div}\.{re.escape(hole)}\.{re.escape(time_of_day)}\.nett"'
                sale_key = rf'name="golf_rate\.rateJson\.{week_div}\.{re.escape(hole)}\.{re.escape(time_of_day)}\.sale\.monkey\.THB"'

                net_val = _extract_val(tb, net_key)
                sale_val = _extract_val(tb, sale_key)

                if (sale_val is None or sale_val == 0) and (net_val is None or net_val == 0):
                    continue

                caddy_status = _extract_status(
                    block,
                    rf'name="golf_rate\.rateJson\.caddy\.{re.escape(hole)}\.{re.escape(time_of_day)}\.caddyStatus"'
                )
                cart_status = _extract_status(
                    block,
                    rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(hole)}\.{re.escape(time_of_day)}\.cartStatus"'
                )

                rows.append({
                    'hole': hole,
                    'time_of_day': time_of_day,
                    'week_div': week_div,
                    'net_thb': net_val or 0,
                    'sale_thb': sale_val or 0,
                    'caddy_net': caddy_net or 0,
                    'caddy_sale_thb': caddy_sale_thb or 0,
                    'cart_net': cart_net or 0,
                    'cart_sale_thb': cart_sale_thb or 0,
                    'caddy_status': caddy_status,
                    'cart_status': cart_status,
                })

    return rows

def _extract_val(html_block: str, name_pattern: str):
    """name 속성으로 input value 추출"""
    pat = name_pattern + r'[^>]*value="([\d,]+)"'
    m = re.search(pat, html_block)
    if m:
        return int(m.group(1).replace(',', ''))
    # value가 앞에 올 수도 있음
    pat2 = r'value="([\d,]+)"[^>]*' + name_pattern
    m2 = re.search(pat2, html_block)
    if m2:
        return int(m2.group(1).replace(',', ''))
    return None


# ─────────────────────────────────────────────
#  HTML 파서: 기간 / 프로모션 메타
# ─────────────────────────────────────────────
def _extract_attr_value(html: str, name_candidates):
    """
    input/textarea 등에서 name/id가 특정 후보일 때 value 텍스트를 최대한 추출.
    """
    for key in name_candidates:
        key_esc = re.escape(key)
        # input value
        m = re.search(rf'(<input[^>]+(?:name|id)="{key_esc}"[^>]*>)', html, flags=re.I)
        if m:
            tag = m.group(1)
            vm = re.search(r'value="([^"]*)"', tag, flags=re.I)
            if vm:
                v = vm.group(1).strip()
                if v:
                    return v
        # textarea content
        tm = re.search(rf'<textarea[^>]+(?:name|id)="{key_esc}"[^>]*>([\s\S]*?)</textarea>', html, flags=re.I)
        if tm:
            v = re.sub(r'\s+', ' ', tm.group(1)).strip()
            if v:
                return v
    return None


def parse_promotion_meta(html: str):
    """
    HTML에서 기간/프로모션명/프로모션정보를 최대한 추정 추출.
    (소스 형태가 다양할 수 있어 여러 후보 키워드를 넓게 탐색)
    """
    def _date_only(v: str | None):
        if not v:
            return None
        # "2025-11-01 00:00:00" → "2025-11-01"
        m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", v)
        return m.group(1) if m else v.strip()

    # ── 골프 레이트 화면에서 쓰는 정확한 키 우선
    start = _extract_attr_value(html, ["golf_rate.startDate"])
    end = _extract_attr_value(html, ["golf_rate.endDate"])

    promo_name_en = _extract_attr_value(html, ["golf_rate.promotionName_en"])
    promo_name_ko = _extract_attr_value(html, ["golf_rate.promotionName_ko"])
    promo_info_en = _extract_attr_value(html, ["golf_rate.promotionInfo_en"])
    promo_info_ko = _extract_attr_value(html, ["golf_rate.promotionInfo_ko"])

    # 상품명: card-header 내 "Supplier : XXX" 텍스트 추출
    supplier_match = re.search(
        r'Supplier\s*:\s*([^<]+)',
        html,
        re.IGNORECASE,
    )
    supplier_name = supplier_match.group(1).strip() if supplier_match else None

    # ── 범용 키워드(폴백)
    promo_name_fallback = _extract_attr_value(
        html,
        [
            "promotionName",
            "promotion_name",
            "promoName",
            "promo_name",
            "promotionTitle",
            "promotion_title",
            "title",
        ],
    )
    promo_info_fallback = _extract_attr_value(
        html,
        [
            "promotionInfo",
            "promotion_info",
            "promoInfo",
            "promo_info",
            "promotionDesc",
            "promotion_desc",
            "description",
            "desc",
            "info",
            "memo",
            "note",
            "notes",
        ],
    )

    start = start or _extract_attr_value(
        html,
        [
            "startDate",
            "start_date",
            "fromDate",
            "from_date",
            "dateFrom",
            "date_from",
            "validFrom",
            "valid_from",
            "periodFrom",
            "period_from",
        ],
    )
    end = end or _extract_attr_value(
        html,
        [
            "endDate",
            "end_date",
            "toDate",
            "to_date",
            "dateTo",
            "date_to",
            "validTo",
            "valid_to",
            "periodTo",
            "period_to",
        ],
    )

    # 키 기반 추출이 실패하면, 날짜 2개를 근처에서 추정
    if not start or not end:
        # yyyy-mm-dd 또는 yyyy/mm/dd 또는 dd/mm/yyyy (간단 커버)
        dates = re.findall(r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", html)
        if len(dates) >= 2:
            start = start or dates[0]
            end = end or dates[1]

    start_d = _date_only(start)
    end_d = _date_only(end)
    period = None
    if start_d and end_d:
        period = f"{start_d} ~ {end_d}"

    def _pack_lang(en: str | None, ko: str | None, fallback: str | None):
        en_v = (en or "").strip() or None
        ko_v = (ko or "").strip() or None
        fb_v = (fallback or "").strip() or None
        return {"en": en_v, "ko": ko_v, "raw": fb_v}

    # 상품명: Supplier 값 하나만 있으면 한글/영문 둘 다 같은 값으로
    product_name = _pack_lang(supplier_name, supplier_name, None) if supplier_name else {"en": None, "ko": None, "raw": None}

    return {
        "period": period,
        "promotion_name": _pack_lang(promo_name_en, promo_name_ko, promo_name_fallback),
        "promotion_info": _pack_lang(promo_info_en, promo_info_ko, promo_info_fallback),
        "product_name": product_name,
    }


# ─────────────────────────────────────────────
#  계산 로직
# ─────────────────────────────────────────────
def _extract_status(html_block: str, name_pattern: str):
    """select의 selected option 텍스트 추출 (caddy/cart status)"""
    # name 패턴으로 select 블록 찾기
    pat = name_pattern + r'[\s\S]*?</select>'
    m = re.search(pat, html_block)
    if not m:
        return None
    select_html = m.group(0)
    # selected option 텍스트 추출
    sel = re.search(r'selected[^>]*>([^<]+)<', select_html)
    if sel:
        return sel.group(1).strip()
    return None


def build_table(rows, exchange_rate, commission_rates, discount_rate, min_margin_rate=0.0):
    """
    rows: parse_golf_html 결과
    반환: DataFrame
    """
    records = []

    for r in rows:
        hole = r['hole']
        time_of_day = r['time_of_day']
        week_div = r['week_div']
        net_thb = r['net_thb']
        sale_thb = r['sale_thb']
        caddy_net = r['caddy_net']
        caddy_sale = r['caddy_sale_thb']
        cart_net = r['cart_net']
        cart_sale = r['cart_sale_thb']

        # 패키지 총 넷가, 세일가
        pkg_net = net_thb + caddy_net + cart_net
        pkg_sale = sale_thb + caddy_sale + cart_sale

        # 원화 환산
        pkg_sale_krw = round(pkg_sale * exchange_rate) if exchange_rate > 0 else 0
        pkg_net_krw = round(pkg_net * exchange_rate) if exchange_rate > 0 else 0

        caddy_status = r.get('caddy_status')
        cart_status = r.get('cart_status')
        caddy_include = caddy_status in ('Include', 'Compulsory') if caddy_status else False
        cart_include = cart_status in ('Include', 'Compulsory') if cart_status else False

        rec = {
            '홀': hole,
            '시간대': time_of_day,
            '주중/주말': '주중' if week_div == 'weekday' else '주말/연휴',
            '그린피(넷, ฿)': net_thb,
            '그린피(세일, ฿)': sale_thb,
            '캐디피(넷, ฿)': caddy_net,
            '카트피(넷, ฿)': cart_net,
            '패키지넷(฿)': pkg_net,
            '패키지세일(฿)': pkg_sale,
            '캐디 포함': '✅ ' + (caddy_status or '') if caddy_include else (caddy_status or '-'),
            '카트 포함': '✅ ' + (cart_status or '') if cart_include else (cart_status or '-'),
        }

        # 원화 환산
        if exchange_rate > 0:
            rec['패키지넷(₩)'] = pkg_net_krw
            rec['패키지세일(₩)'] = pkg_sale_krw

        # 수수료별 계산
        discount = discount_rate / 100
        for comm in commission_rates:
            comm_d = comm / 100
            comm_str = str(comm).replace('.', '_')

            # 판매가 = 패키지세일(₩), 공급가 = 판매가 × (1 - 수수료율)
            final_price_krw = pkg_sale_krw
            supply_krw = round(final_price_krw * (1 - comm_d)) if exchange_rate > 0 else 0
            commission_krw = final_price_krw - supply_krw
            margin_krw = supply_krw - pkg_net_krw

            # 조정 판매가/공급가/마진 역산
            # 조정은 마진이 음수일 때만 적용 (마진이 '+'일 때는 적용하지 않음)
            need_adjust = (margin_krw < 0) and exchange_rate > 0 and (1 - comm_d) > 0
            if need_adjust:
                # 조정공급가 = 패키지넷 / (1 - 목표마진율%)  → 공급가 대비 마진 비율
                adj_supply_krw = math.ceil(pkg_net_krw / (1 - min_margin_rate / 100))
                # 조정판매가 = 조정공급가 ÷ (1 - 수수료율)
                target_final_krw = math.ceil(adj_supply_krw / (1 - comm_d))
                # 조정마진 = 조정공급가 - 패키지넷(₩)
                adj_margin_krw = adj_supply_krw - pkg_net_krw
            else:
                # 목표마진율 미입력이고 마진 ≥ 0 → 조정 불필요, 모두 0
                target_final_krw = 0
                adj_supply_krw = 0
                adj_margin_krw = 0

            if exchange_rate > 0:
                rec[f'판매가_{comm_str}%(₩)'] = final_price_krw
                rec[f'공급가_{comm_str}%(₩)'] = supply_krw
                rec[f'마진_{comm_str}%(₩)'] = margin_krw
                rec[f'조정판매가_{comm_str}%(₩)'] = target_final_krw
                rec[f'조정공급가_{comm_str}%(₩)'] = adj_supply_krw
                rec[f'조정마진_{comm_str}%(₩)'] = adj_margin_krw

        records.append(rec)

    return pd.DataFrame(records)


# ─────────────────────────────────────────────
#  스타일
# ─────────────────────────────────────────────
BOLD_PREFIXES = ('판매가_', '공급가_', '마진_', '조정판매가_', '조정공급가_', '조정마진_')

def style_df(df):
    # 볼드 처리할 컬럼 인덱스
    bold_cols = {i for i, col in enumerate(df.columns) if col.startswith(BOLD_PREFIXES)}

    def highlight(row):
        styles = [''] * len(row)

        # 지정 컬럼만 굵게
        for i in bold_cols:
            styles[i] = 'font-weight: bold'

        # 마진 마이너스 → 행 전체 빨강
        for i, col in enumerate(row.index):
            if '마진' in col:
                try:
                    v = float(str(row[col]).replace(',', '').replace('원', ''))
                    if v < 0:
                        return ['background-color: #fee2e2; color: #dc2626; font-weight: bold'] * len(row)
                except:
                    pass

        # 주말/연휴 행 → 연한 노랑 (굵기 유지)
        if '주중/주말' in row.index and row['주중/주말'] == '주말/연휴':
            return [s + '; background-color: #fefce8' if s else 'background-color: #fefce8' for s in styles]

        return styles

    return df.style.apply(highlight, axis=1)


# ─────────────────────────────────────────────
#  UI
# ─────────────────────────────────────────────
def main():
    st.title("⛳ 골프 요금 마크업 계산기")
    st.markdown("골프 요금표 HTML을 붙여넣으면 패키지 요금 + 환율 + 수수료를 자동 계산합니다.")

    # session_state 초기화
    for key, default in [
        ('html_key', 0), ('result_df', None),
        ('exchange_rate', 0.0), ('commission_rates', []),
        ('min_margin_rate', 0.0),
        ('html_blocks', 1),
        ('results', None),
        ('scroll_to_results', False),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── 파라미터 입력 (상단)
    col1, col2, col3 = st.columns(3)
    with col1:
        exchange_input = st.text_input("환율 (THB → KRW)", placeholder="예: 43.5", value="")
    with col2:
        commission_input = st.text_input("수수료 (%)", placeholder="예: 4,6.6,10", value="")
    with col3:
        st.write("")
        st.write("")
        calc_btn = st.button("🔢 계산하기", type="primary", use_container_width=True)

    # ── HTML 입력 영역 (아래): 제목 → 캡션 → ➕ 추가 버튼
    st.markdown("### 골프 요금표 HTML 붙여넣기")
    st.caption("추가 버튼으로 입력칸을 최대 5개까지 추가할 수 있어요.")
    add_clicked = st.button("➕ 추가", use_container_width=False, help="HTML 입력칸 추가 (최대 5개)")
    if add_clicked:
        st.session_state['html_blocks'] = min(5, int(st.session_state.get('html_blocks', 1)) + 1)
        st.rerun()

    col_input, col_clear = st.columns([5, 1])
    with col_input:
        html_inputs = []
        blocks = int(st.session_state.get('html_blocks', 1))
        for i in range(blocks):
            html_inputs.append(
                st.text_area(
                    f"HTML #{i+1}",
                    placeholder="<div class=\"table-responsive table-fixed-rate\">...",
                    height=200,
                    key=f"html_input_{st.session_state['html_key']}_{i}",
                )
            )
    with col_clear:
        st.write("")
        st.write("")
        if st.button("🗑️ Clear", use_container_width=True, help="입력/결과 전체 초기화"):
            st.session_state['html_key'] += 1
            st.session_state['result_df'] = None
            st.session_state['results'] = None
            st.session_state['min_margin_rate'] = 0.0
            st.session_state['html_blocks'] = 1
            st.rerun()

    # ── 계산 실행
    if calc_btn:
        valid_htmls = [h for h in html_inputs if h and h.strip()]
        if not valid_htmls:
            st.error("HTML을 입력해 주세요. (최소 1개)")
        else:
            try:
                exchange_rate = float(exchange_input.strip()) if exchange_input.strip() else 0.0
            except:
                exchange_rate = 0.0
                st.warning("환율 형식 오류 → 0으로 처리")

            try:
                commission_rates = [float(x.strip()) for x in commission_input.split(',') if x.strip()]
            except:
                commission_rates = []
                st.warning("수수료 형식 오류 → 빈 리스트로 처리")

            discount_rate = 0.0
            min_margin_rate = 0.0
            results = []
            total_rows = 0
            for idx, html_input in enumerate(valid_htmls, start=1):
                rows = parse_golf_html(html_input)
                meta = parse_promotion_meta(html_input)
                if not rows:
                    results.append({
                        "idx": idx,
                        "error": "HTML에서 골프 요금 데이터를 찾지 못했습니다. 올바른 골프 요금표 HTML인지 확인해 주세요.",
                        "meta": meta,
                        "rows": None,
                        "df": None,
                        "html": html_input,
                    })
                    continue

                df = build_table(rows, exchange_rate, commission_rates, 0.0, min_margin_rate)
                total_rows += len(rows)
                results.append({
                    "idx": idx,
                    "error": None,
                    "meta": meta,
                    "rows": rows,   # 마진율 재적용 시 재계산용
                    "df": df,
                    "html": html_input,
                })

            st.session_state['results'] = results
            st.session_state['result_df'] = None  # 단일 DF 경로는 사용하지 않음(호환용 키)
            st.session_state['exchange_rate'] = exchange_rate
            st.session_state['commission_rates'] = commission_rates
            st.session_state['min_margin_rate'] = min_margin_rate
            st.session_state['scroll_to_results'] = True
            st.success(f"✅ 총 {len(valid_htmls)}개 HTML 처리 완료! (파싱된 요금 항목 합계: {total_rows}개)")
            st.rerun()

    # ── 결과 표시
    if st.session_state.get('results'):
        exchange_rate = st.session_state['exchange_rate']
        commission_rates = st.session_state['commission_rates']
        min_margin_rate = st.session_state.get('min_margin_rate', 0.0)

        # 결과 영역 앵커 + 계산하기 직후면 여기로 스크롤 (components.html로 스크립트 실행)
        st.markdown('<div id="result-section"></div>', unsafe_allow_html=True)
        if st.session_state.get('scroll_to_results'):
            st.session_state['scroll_to_results'] = False
            scroll_js = """
            <script>
            setTimeout(function() {
                var doc = (window.parent && window.parent.document) ? window.parent.document : document;
                var el = doc.getElementById('result-section');
                if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
            }, 300);
            </script>
            """
            components.html(scroll_js, height=0)
        st.markdown("---")

        # 상품명 (첫 번째 결과 기준, 환율 바로 위) — card-header "Supplier : XXX" 추출값, 영문만 표시
        first_meta = (st.session_state.get("results") or [{}])[0].get("meta") or {}
        product_name = first_meta.get("product_name") or {}
        pname_en = (product_name.get("en") or "").strip() or "-"
        pname_esc = html.escape(pname_en)
        st.markdown("**상품명**")
        st.markdown(f'<div style="font-size:3rem; margin-bottom:1.5rem;">{pname_esc}</div>', unsafe_allow_html=True)

        # 설정 요약 (공통)
        c1, c2, c3 = st.columns(3)
        c1.metric("환율", f"1 THB = {exchange_rate:,.2f} KRW" if exchange_rate > 0 else "미설정")
        c2.metric("수수료", ", ".join([f"{x}%" for x in commission_rates]) if commission_rates else "미설정")
        c3.metric("목표 마진율", f"{min_margin_rate:.2f}%" if min_margin_rate and min_margin_rate > 0 else "미설정")

        # ── 목표 마진율 입력 (결과 상단, 전체 결과에 적용)
        st.markdown("### 마진 설정")
        col_margin, col_apply = st.columns([2, 1])
        with col_margin:
            margin_input_val = st.text_input(
                "목표 마진율 (%)",
                help="공급가 대비 마진 비율. 예) 10% 입력 시 → 조정공급가의 10%를 마진으로 확보. 미입력 시 마진 0 기준(손익분기)으로 계산됩니다.",
                value=str(st.session_state.get('min_margin_rate', 0.0)) if st.session_state.get('min_margin_rate', 0.0) > 0 else "",
                placeholder="미입력 시 조정마진 0 (손익분기 기준)",
                key="margin_rate_input"
            )
        with col_apply:
            st.write("　")
            if st.button("✅ 마진율 적용", use_container_width=True):
                try:
                    new_margin = float(margin_input_val.strip()) if margin_input_val.strip() else 0.0
                except:
                    new_margin = 0.0
                st.session_state['min_margin_rate'] = new_margin
                # 각 HTML 결과를 다시 계산
                new_results = []
                for r in st.session_state.get('results', []):
                    if r.get("error") or not r.get("rows"):
                        new_results.append(r)
                        continue
                    df = build_table(
                        r["rows"],
                        st.session_state['exchange_rate'],
                        st.session_state['commission_rates'],
                        0.0,
                        new_margin
                    )
                    rr = dict(r)
                    rr["df"] = df
                    new_results.append(rr)
                st.session_state['results'] = new_results
                st.rerun()

        # ── 결과별 표시
        for res in st.session_state.get('results', []):
            idx = res.get("idx", 1)
            meta = res.get("meta") or {}

            st.markdown("---")
            st.markdown(f"## 결과 #{idx}")

            pn = meta.get("promotion_name") or {}
            pi = meta.get("promotion_info") or {}
            pn_text = (
                " / ".join([x for x in [(pn.get("en") or "-"), (pn.get("ko") or "-")] if x])
                if isinstance(pn, dict)
                else (pn or "-")
            )
            pi_text = (
                " / ".join([x for x in [(pi.get("en") or "-"), (pi.get("ko") or "-")] if x])
                if isinstance(pi, dict)
                else (pi or "-")
            )

            period_val = meta.get("period") or "-"
            period_esc = html.escape(period_val)
            pn_esc = html.escape(pn_text)
            pi_esc = html.escape(pi_text)
            st.markdown(
                f'<div style="margin-bottom:1rem;">'
                f'<div style="font-size:1.5rem; color:#6b7280;">기간</div>'
                f'<div style="font-size:1.75em;">{period_esc}</div></div>'
                f'<div style="margin-bottom:1rem;">'
                f'<div style="font-size:1.5rem; color:#6b7280;">Promotion name</div>'
                f'<div style="font-size:1.75em;">{pn_esc}</div></div>'
                f'<div style="margin-bottom:1rem;">'
                f'<div style="font-size:1.5rem; color:#6b7280;">Promotion info</div>'
                f'<div style="font-size:1.75em;">{pi_esc}</div></div>',
                unsafe_allow_html=True,
            )

            if res.get("error"):
                st.error(res["error"])
                continue

            df: pd.DataFrame = res["df"]

            st.markdown(f"### 요금표 ({len(df)}개 항목)")

            # 숫자 컬럼 포맷팅 (표시용 복사본)
            display_df = df.copy()
            for col in display_df.columns:
                if any(k in col for k in ['฿)', '(฿', '₩)', '(₩']):
                    display_df[col] = display_df[col].apply(
                        lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x
                    )

            styled = style_df(display_df)

            # 컬럼명 볼드 처리 CSS
            bold_prefixes = ('판매가_', '공급가_', '마진_', '조정판매가_', '조정공급가_', '조정마진_')
            bold_col_indices = [i for i, col in enumerate(display_df.columns) if col.startswith(bold_prefixes)]
            if bold_col_indices:
                css_rules = " ".join([
                    f'div[data-testid="stDataFrame"] thead tr th:nth-child({i+2}) div {{ font-weight: 900 !important; }}'
                    for i in bold_col_indices
                ])
                st.markdown(f"<style>{css_rules}</style>", unsafe_allow_html=True)

            row_height_px = 35
            header_height_px = 48
            table_height = min(600, header_height_px + row_height_px * len(df))
            st.dataframe(styled, use_container_width=True, height=table_height)

            # CSV 다운로드 / 구글 스프레드시트 내보내기
            st.markdown("---")
            csv = df.to_csv(index=False, encoding='utf-8-sig')
            dl_col, gs_col = st.columns(2)
            with dl_col:
                st.download_button(
                    label="📥 CSV 다운로드",
                    data=csv,
                    file_name=f"golf_markup_result_{idx}.csv",
                    mime="text/csv",
                    key=f"csv_dl_{idx}",
                )
            with gs_col:
                if GSPREAD_AVAILABLE:
                    sheet_id_input = st.text_input(
                        "구글 스프레드시트 ID 또는 URL",
                        placeholder="예: 1ABC...xyz 또는 https://docs.google.com/spreadsheets/d/1ABC.../edit",
                        key=f"sheet_id_{idx}",
                    )
                    if st.button("📤 내보내기", key=f"export_gs_{idx}"):
                        if not sheet_id_input or not sheet_id_input.strip():
                            st.error("스프레드시트 ID 또는 URL을 입력해 주세요.")
                        else:
                            raw = sheet_id_input.strip()
                            # URL에서 ID 추출
                            if "/d/" in raw:
                                sid = raw.split("/d/")[1].split("/")[0].split("?")[0]
                            else:
                                sid = raw
                            try:
                                meta = res.get("meta") or {}
                                pname = (meta.get("product_name") or {}).get("en") or ""
                                period = meta.get("period") or ""
                                with st.spinner("구글 스프레드시트로 내보내는 중..."):
                                    export_df_to_google_sheets(
                                        df, sid,
                                        sheet_name="요금표",
                                        product_name=pname,
                                        period=period,
                                    )
                                st.success("구글 스프레드시트로 내보냈습니다.")
                            except Exception as e:
                                st.error(f"내보내기 실패: {e}")
                else:
                    st.caption("내보내기: pip install gspread google-auth 후 시크릿 설정 필요")


if __name__ == "__main__":
    main()
