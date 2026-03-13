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


def _parse_period_end(period_str: str):
    """기간 문자열에서 종료일 파싱. 예: '2025-11-26 ~ 2026-03-31' -> 2026-03-31."""
    if not period_str or not isinstance(period_str, str):
        return None
    m = re.search(r"~\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*$", period_str.strip())
    if not m:
        return None
    s = m.group(1).replace("/", "-")
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def export_all_results_to_google_sheets(
    results,
    spreadsheet_id: str,
    sheet_name: str = "요금표",
    extracted_date: str | None = None,
    exchange_rate: float = 0.0,
):
    """여러 개의 요금표(results 리스트)를 한 번에 내보냅니다.

    - 각 요금표 블록 위에 구분선 행 추가 (첫 블록 위: 진한 회색, 사이: 연한 회색)
    - 요금표가 2개 이상이면 마지막 2개를 비교해 '판매가 증가율' = (A/B - 1)*100% 를 마지막 열에 추가
      (A=기간이 최신인 쪽 판매가, B=기간이 더 이전인 쪽 판매가)
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
        worksheet = sh.add_worksheet(title=sheet_name, rows=500, cols=30)

    try:
        header_row = worksheet.row_values(1)
    except Exception:
        header_row = []
    if len(header_row) < 1 or (header_row[0] or "").strip() != "상품명":
        worksheet.update_acell("A1", "상품명")
    if len(header_row) < 3 or (header_row[2] or "").strip() != "기간":
        worksheet.update_acell("C1", "기간")

    all_vals = worksheet.get_all_values()
    next_row = len(all_vals) + 1 if all_vals else 2
    extracted_date = extracted_date or datetime.date.today().isoformat()

    # 유효한 요금표만 (df 있음)
    valid_results = []
    for res in results or []:
        df = (res or {}).get("df")
        if df is None or df.empty:
            continue
        valid_results.append(res)

    # 요금표 2개 이상일 때: 마지막 2개 비교 -> (key -> (A, B)) 맵. A=최신 판매가, B=이전 판매가
    increase_rate_map = {}  # key (홀, 시간대, 주중/주말) -> (A, B) then rate = (A/B - 1)*100
    sale_col = None  # 판매가 컬럼명 (첫 번째 '판매가_' 로 시작하는 컬럼)
    num_export_cols = 24  # 기본: A~X (추출날짜 + 환율)

    if len(valid_results) >= 2:
        res1, res2 = valid_results[-2], valid_results[-1]
        df1, df2 = res1.get("df"), res2.get("df")
        meta1, meta2 = res1.get("meta") or {}, res2.get("meta") or {}
        period1, period2 = meta1.get("period") or "", meta2.get("period") or ""
        end1, end2 = _parse_period_end(period1), _parse_period_end(period2)
        # 기간이 최신인 쪽 = A, 그렇지 않은 쪽 = B
        if end2 and end1 and end2 >= end1:
            df_old, df_new = df1, df2
        else:
            df_old, df_new = df2, df1

        for c in df_new.columns:
            if str(c).startswith("판매가_"):
                sale_col = c
                break
        # 조정판매가가 있으면(0이 아니면) 조정판매가, 없으면 판매가 사용 (같은 수수료 컬럼 대응)
        adj_col = None
        if sale_col:
            suffix = str(sale_col).replace("판매가_", "", 1)
            adj_col = f"조정판매가_{suffix}"
            if adj_col not in df_new.columns or adj_col not in df_old.columns:
                adj_col = None

        def _price_for_row(dframe, i, sale_c, adj_c):
            if adj_c and adj_c in dframe.columns:
                try:
                    v = float(dframe.iloc[i][adj_c])
                    if v and v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
            try:
                return float(dframe.iloc[i][sale_c]) if sale_c in dframe.columns else None
            except (TypeError, ValueError):
                return None

        if sale_col and sale_col in df_old.columns and sale_col in df_new.columns:
            key_cols = ["홀", "시간대", "주중/주말"]
            if all(k in df_old.columns and k in df_new.columns for k in key_cols):
                old_by_key = {}
                for i in range(len(df_old)):
                    key = tuple(df_old.iloc[i][k] for k in key_cols)
                    p = _price_for_row(df_old, i, sale_col, adj_col)
                    if p is not None:
                        old_by_key[key] = p
                for i in range(len(df_new)):
                    key = tuple(df_new.iloc[i][k] for k in key_cols)
                    A = _price_for_row(df_new, i, sale_col, adj_col)
                    B = old_by_key.get(key)
                    if A is not None and B is not None and B != 0:
                        rate = (A / B - 1) * 100
                        increase_rate_map[key] = (A, B, rate)
        num_export_cols = 25  # 마지막 열에 '판매가 증가율' 추가 (추출날짜 + 환율 + 판매가 증가율)

    rows = []
    dark_sep_rows = []
    light_sep_rows = []
    first_block = True

    for res in results or []:
        df = (res or {}).get("df")
        if df is None or df.empty:
            continue
        meta = (res or {}).get("meta") or {}
        product_name = (meta.get("product_name") or {}).get("en") or ""
        period = meta.get("period") or ""

        sep_row = [""] * num_export_cols
        sep_row_index = next_row + len(rows)
        rows.append(sep_row)
        if first_block:
            dark_sep_rows.append(sep_row_index)
            first_block = False
        else:
            light_sep_rows.append(sep_row_index)

        df_str = df.astype(str)
        key_cols = ["홀", "시간대", "주중/주말"]
        has_keys = all(k in df.columns for k in key_cols) if key_cols else False
        # 가장 최신 요금표(마지막 블록)에만 판매가 증가율 표시
        is_newest_block = len(valid_results) >= 2 and res is valid_results[-1]

        for i in range(len(df_str)):
            row = [product_name, "", period] + df_str.iloc[i].tolist()
            # W열(22번 인덱스) = 추출날짜
            if len(row) <= 22:
                row.extend([""] * (23 - len(row)))
            row[22] = extracted_date
            # X열 = 환율 (추출날짜 바로 뒤)
            row.append(str(exchange_rate) if exchange_rate else "")
            # Y열 = 판매가 증가율 (가장 최신 요금표 행에만, 요금표 2개 이상일 때)
            if num_export_cols >= 25:
                if is_newest_block and has_keys and increase_rate_map:
                    key = tuple(df.iloc[i][k] for k in key_cols)
                    t = increase_rate_map.get(key)
                    if t is not None:
                        row.append(f"{t[2]:.2f}%")
                    else:
                        row.append("")
                else:
                    row.append("")
            rows.append(row)

    if not rows:
        return True

    needed_rows = next_row + len(rows) - 1
    if worksheet.row_count < needed_rows:
        worksheet.add_rows(needed_rows - worksheet.row_count)

    start_cell = f"A{next_row}"
    worksheet.update(range_name=start_cell, values=rows, value_input_option="USER_ENTERED")

    # 헤더 1행: X1 = 환율, Y1 = 판매가 증가율(요금표 2개 이상일 때만)
    try:
        h = worksheet.row_values(1)
        if len(h) < 24 or (h[23] or "").strip() != "환율":
            worksheet.update_acell("X1", "환율")
    except Exception:
        worksheet.update_acell("X1", "환율")
    if num_export_cols >= 25:
        try:
            h = worksheet.row_values(1)
            if len(h) < 25 or (h[24] or "").strip() != "판매가 증가율":
                worksheet.update_acell("Y1", "판매가 증가율")
        except Exception:
            worksheet.update_acell("Y1", "판매가 증가율")

    def _format_rows(idxs, bg):
        if not idxs:
            return
        last_col = "Y" if num_export_cols >= 25 else "X"
        for r in idxs:
            rng = f"A{r}:{last_col}{r}"
            worksheet.format(
                rng,
                {
                    "backgroundColor": {"red": bg[0], "green": bg[1], "blue": bg[2]},
                    "textFormat": {"bold": True},
                },
            )

    _format_rows(dark_sep_rows, (0.4, 0.4, 0.4))
    _format_rows(light_sep_rows, (0.9, 0.9, 0.9))
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


def build_table(rows, exchange_rate, commission_rates, discount_rate, min_margin_rate=0.0, is_marit: bool = False):
    """
    rows: parse_golf_html 결과
    반환: DataFrame
    """
    records = []

    for r in rows:
        hole = r['hole']
        time_of_day = r['time_of_day']
        # 마리트 전용 필터: 27H 제외, 18H/36H만 + Morning/Afternoon만
        if is_marit:
            hole_str = str(hole).strip()
            time_str = str(time_of_day).strip()
            if hole_str not in ("18H", "36H"):
                continue
            if time_str not in ("Morning", "Afternoon"):
                continue
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
#  요금표 편집 후 공급가/마진 재계산
# ─────────────────────────────────────────────
def _comm_from_col_name(col_name: str, prefix: str) -> float | None:
    """컬럼명에서 수수료율 추출. 예: '판매가_4_0%(₩)' -> 4.0"""
    if not col_name.startswith(prefix) or "%(₩)" not in col_name:
        return None
    try:
        mid = col_name[len(prefix) : col_name.index("%(₩)")]
        return float(mid.replace("_", ".", 1))
    except (ValueError, TypeError):
        return None


def apply_price_edits(df: pd.DataFrame) -> pd.DataFrame:
    """판매가/조정판매가 변경분을 반영해 공급가·마진·조정공급가·조정마진을 재계산합니다.
    조정판매가/조정공급가/조정마진은 '마진'이 음수인 행에서만 적용하고, 마진이 0 이상인 행은 0으로 둡니다.
    """
    out = df.copy()
    if "패키지넷(₩)" not in out.columns:
        return out
    pkg_net = out["패키지넷(₩)"].astype(float, errors="ignore").fillna(0)

    # 1) 판매가 → 공급가, 마진 재계산
    for col in list(out.columns):
        if col.startswith("판매가_") and "(₩)" in col:
            rate = _comm_from_col_name(col, "판매가_")
            if rate is None:
                continue
            suffix = col.replace("판매가_", "", 1)
            supply_col = f"공급가_{suffix}"
            margin_col = f"마진_{suffix}"
            if supply_col not in out.columns or margin_col not in out.columns:
                continue
            sale = pd.to_numeric(out[col], errors="coerce").fillna(0)
            supply = (sale * (1 - rate / 100)).round()
            out[supply_col] = supply.astype(int)
            out[margin_col] = (supply - pkg_net).astype(int)

    # 2) 조정판매가 → 조정공급가, 조정마진은 '마진'이 음수인 행에서만 적용
    for col in list(out.columns):
        if not col.startswith("조정판매가_") or "(₩)" not in col:
            continue
        rate = _comm_from_col_name(col, "조정판매가_")
        if rate is None:
            continue
        suffix = col.replace("조정판매가_", "", 1)
        margin_col = f"마진_{suffix}"
        adj_supply_col = f"조정공급가_{suffix}"
        adj_margin_col = f"조정마진_{suffix}"
        if margin_col not in out.columns or adj_supply_col not in out.columns or adj_margin_col not in out.columns:
            continue
        margin_vals = pd.to_numeric(out[margin_col], errors="coerce").fillna(0)
        adj_sale = pd.to_numeric(out[col], errors="coerce").fillna(0)
        adj_supply = (adj_sale * (1 - rate / 100)).round()
        # 마진 < 0 인 행만 조정값 반영, 나머지는 0
        mask_neg = margin_vals < 0
        out[adj_supply_col] = 0
        out.loc[mask_neg, adj_supply_col] = adj_supply.loc[mask_neg].astype(int)
        out[adj_margin_col] = 0
        out.loc[mask_neg, adj_margin_col] = (adj_supply.loc[mask_neg] - pkg_net.loc[mask_neg]).astype(int)
        out[col] = 0
        out.loc[mask_neg, col] = adj_sale.loc[mask_neg]

    return out


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

    # 체크박스 라벨 / 버튼 스타일 살짝 키우기 (마리트 옵션 포함, 공통 버튼 스타일)
    st.markdown(
        """
        <style>
        div[data-testid="stCheckbox"] label span {
            font-size: 1.05rem;
            font-weight: 600;
        }
        div.stButton > button {
            background-color: #2563eb !important;  /* 파란색 */
            color: white !important;
            border-radius: 8px;
            padding: 0.5rem 1.5rem;
            font-size: 1rem;
            font-weight: 600;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

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
        exchange_input = st.text_input("환율 (THB → KRW)", placeholder="예: 43.5", value="48.5")
    with col2:
        commission_input = st.text_input("수수료 (%)", placeholder="예: 4,6.6,10", value="")
    with col3:
        st.write("")
        st.write("")
        calc_btn = st.button("🔢 계산하기", type="primary", use_container_width=True)

    # 마리트 전용 필터 옵션
    is_marit = st.checkbox(
        "마리트 (27H 제외, Night 제외)",
        help="체크하면 홀은 18H/36H만, 시간대는 Morning/Afternoon만 표시됩니다.",
        key="is_marit",
    )

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
            is_marit = st.session_state.get('is_marit', False)
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

                df = build_table(rows, exchange_rate, commission_rates, 0.0, min_margin_rate, is_marit=is_marit)
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
                        new_margin,
                        is_marit=st.session_state.get('is_marit', False),
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
            st.caption("판매가·조정판매가만 수정 가능합니다. 수정 시 해당 행의 공급가·마진이 자동으로 다시 계산됩니다.")

            # 편집 가능 컬럼: 판매가_*, 조정판매가_* 만. 나머지는 읽기 전용
            column_config = {}
            for col in df.columns:
                if (col.startswith("판매가_") or col.startswith("조정판매가_")) and "(₩)" in col:
                    column_config[col] = st.column_config.NumberColumn(col, format="%d")
                else:
                    column_config[col] = st.column_config.Column(col, disabled=True)

            edited_df = st.data_editor(
                df,
                key=f"fee_editor_{idx}",
                column_config=column_config,
                use_container_width=True,
                height=min(600, 48 + 35 * len(df)),
            )

            # 편집 후 항상 공급가·마진(·조정공급가·조정마진) 재계산, 변경 시 세션 반영
            recalc_df = apply_price_edits(edited_df)
            same = False
            try:
                if recalc_df.shape == df.shape and list(recalc_df.columns) == list(df.columns):
                    same = True
                    for c in df.columns:
                        if pd.api.types.is_numeric_dtype(df[c]) and pd.api.types.is_numeric_dtype(recalc_df[c]):
                            if not pd.to_numeric(df[c], errors="coerce").fillna(0).round(2).equals(
                                pd.to_numeric(recalc_df[c], errors="coerce").fillna(0).round(2)
                            ):
                                same = False
                                break
                        elif not df[c].astype(str).eq(recalc_df[c].astype(str)).all():
                            same = False
                            break
            except Exception:
                same = False
            if not same:
                new_results = list(st.session_state.get("results") or [])
                for j, r in enumerate(new_results):
                    if r.get("idx") == idx:
                        new_results[j] = {**r, "df": recalc_df}
                        break
                st.session_state["results"] = new_results
                st.rerun()

            # CSV 다운로드 / 구글 스프레드시트 내보내기
            st.markdown("---")
            csv = df.to_csv(index=False, encoding='utf-8-sig')
            st.download_button(
                label="📥 CSV 다운로드",
                data=csv,
                file_name=f"golf_markup_result_{idx}.csv",
                mime="text/csv",
                key=f"csv_dl_{idx}",
            )

        # ── 모든 요금표 한 번에 구글 시트로 내보내기 (최하단)
        st.markdown("---")
        st.markdown("### 내보내기")
        has_gcp_secret = bool(st.secrets.get("gcp_service_account"))
        # TODO: 나중에 st.secrets["golf_sheet_url"]로 변경 예정
        sheet_url = "https://docs.google.com/spreadsheets/d/1qDp5Ty_NnQgYKfyhOnV0l5q7v8TPFtvsIi91GirO180/edit?gid=0#gid=0"
        if GSPREAD_AVAILABLE and has_gcp_secret:
            st.caption("모든 요금표를 한 번에 구글 시트로 내보냅니다. (맨 위 진한 회색, 각 요금표 사이 연한 회색 구분선 추가)")
            col_l, col_btn, col_r = st.columns([2, 1, 2])
            with col_btn:
                if st.button("📤 전체 요금표 구글 시트로 내보내기"):
                    raw = str(sheet_url).strip()
                    if "/d/" in raw:
                        sid = raw.split("/d/")[1].split("/")[0].split("?")[0]
                    else:
                        sid = raw
                    try:
                        with st.spinner("구글 스프레드시트로 모든 요금표를 내보내는 중..."):
                            export_all_results_to_google_sheets(
                                st.session_state.get("results") or [],
                                sid,
                                sheet_name="요금표",
                                exchange_rate=st.session_state.get("exchange_rate", 0.0),
                            )
                        st.success("모든 요금표를 구글 스프레드시트로 내보냈습니다.")
                    except Exception as e:
                        st.error(f"내보내기 실패: {e}")
        else:
            st.caption("구글 시트 내보내기를 사용하려면 gspread 설치 및 서비스계정 시크릿 설정이 필요합니다.")


if __name__ == "__main__":
    main()
