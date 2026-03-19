import streamlit as st
import streamlit.components.v1 as components
import re
import pandas as pd
import math
import html
import datetime
import requests
import json as _json

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

st.set_page_config(page_title="골프 요금 마크업 계산기", layout="wide")

# 플랫폼 유형: (value, 라디오 라벨, 구글 시트 탭 이름)
GOLF_MODES = [
    ("mrt", "마이리얼트립 (27H 제외, Night 제외)", "마리트 골프"),
    ("kakao", "카카오 골프", "카카오 골프"),
    ("triple", "트리플 골프", "트리플 골프"),
]

# 도시 ID → 한글 도시명 매핑
CITY_MAP: dict[int, str] = {
    1:   "방콕",
    5:   "파타야",
    6:   "푸켓",
    20:  "꼬 사무이",
    21:  "치앙마이",
    22:  "후아힌/차암",
    34:  "라용/꼬 싸멧",
    37:  "꼬 창",
    55:  "칸차나부리",
    92:  "북부지방",
    113: "끄라비",
    114: "카오락/팡아",
    282: "꼬 팡안/꼬 따오",
    283: "치앙라이",
    717: "꼬 사무이 SHA+",
    756: "카오야이",
    770: "라오스",
}

def _city_name(city_id) -> str:
    try:
        return CITY_MAP.get(int(city_id), "") if city_id is not None else ""
    except Exception:
        return ""


def _golf_mode_from_label(selected_label: str):
    for value, label, sheet_name in GOLF_MODES:
        if label == selected_label:
            return value, sheet_name
    return GOLF_MODES[0][0], GOLF_MODES[0][2]


# =============================================================================
# n8n webhook 호출 헬퍼
# =============================================================================

def _n8n_base_url() -> str:
    return (st.secrets.get("n8n") or {}).get("base_url") or "https://n8n.monkeytravel.com"


def _webhook_get(path: str, params: dict) -> dict:
    """n8n webhook GET 호출 (query string)"""
    try:
        r = requests.get(
            f"{_n8n_base_url()}/webhook/{path}",
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"webhook 오류 ({path}): {e}")
        return {}


# =============================================================================
# 검색 함수 (n8n webhook 경유)
# =============================================================================

def search_golf_products(keyword: str) -> list[dict]:
    """
    n8n webhook → ES product 인덱스 검색
    webhook: GET /webhook/golf-product-search?keyword=...
    반환: [{product_id, name_ko, name_en, grade, address, is_golf}]
    """
    data = _webhook_get("golf-product-search", {"keyword": keyword})
    results = data.get("results") or []
    # statistics_city_id → 한글 도시명 추가
    for r in results:
        r["city_ko"] = _city_name(r.get("city_id"))
    return results


def fetch_golf_rates_by_product(product_id: int) -> list[dict]:
    """
    n8n webhook → ES golf_rate 인덱스 조회
    webhook: GET /webhook/golf-rate-fetch?product_id=...
    반환: [{id, startDate, endDate, promotionName_ko/en, promotionInfo_ko/en, rateJson}]
    """
    data = _webhook_get("golf-rate-fetch", {"product_id": product_id})
    return data.get("rates") or []


# =============================================================================
# rateJson → build_table() 용 rows 변환
# =============================================================================

def parse_rate_json(rate_json_raw) -> list[dict]:
    """
    golf_rate.rateJson (str 또는 dict) 파싱 →
    build_table()이 받는 rows 리스트 반환.
    """
    if not rate_json_raw:
        return []
    try:
        rj = _json.loads(rate_json_raw) if isinstance(rate_json_raw, str) else rate_json_raw
    except Exception:
        return []

    def _num(v):
        if v is None or v == "" or isinstance(v, (list, dict)):
            return 0
        try:
            return float(str(v).replace(",", ""))
        except Exception:
            return 0

    def _is_empty(v):
        return v is None or v == [] or v == {}

    caddy_sec = rj.get("caddy", {}) or {}
    cart_sec  = rj.get("cart1pax", {}) or {}
    wd_data   = rj.get("weekday", {}) or {}
    we_data   = rj.get("weekend", {}) or {}

    TIME_SLOTS = ["Morning", "Afternoon", "Twilight", "Night"]
    rows = []
    all_holes = sorted(set(list(wd_data.keys()) + list(we_data.keys())))

    for hole in all_holes:
        wd_hole = wd_data.get(hole) or {}
        we_hole = we_data.get(hole) or {}

        caddy_h   = caddy_sec.get(hole) or {}
        cart_h    = cart_sec.get(hole)  or {}
        caddy_net = _num(caddy_h.get("nett"))
        caddy_thb = _num((caddy_h.get("sale") or {}).get("THB"))
        cart_net  = _num(cart_h.get("nett"))
        cart_thb  = _num((cart_h.get("sale") or {}).get("THB"))

        for time_of_day in TIME_SLOTS:
            wd_t = wd_hole.get(time_of_day)
            we_t = we_hole.get(time_of_day)

            if _is_empty(wd_t) and _is_empty(we_t):
                continue

            def _status(section_hole, time, key):
                t = section_hole.get(time)
                return t.get(key) if isinstance(t, dict) else None

            caddy_status = _status(caddy_h, time_of_day, "caddyStatus")
            cart_status  = _status(cart_h,  time_of_day, "cartStatus")

            for week_div, time_data in [("weekday", wd_t), ("weekend", we_t)]:
                if _is_empty(time_data) or not isinstance(time_data, dict):
                    continue

                net_thb = _num(time_data.get("nett"))
                sale_mk = _num((time_data.get("sale") or {}).get("monkey", {}).get("THB"))

                if net_thb == 0 and sale_mk == 0:
                    continue

                rows.append({
                    "hole":           hole,
                    "time_of_day":    time_of_day,
                    "week_div":       week_div,
                    "net_thb":        net_thb,
                    "sale_thb":       sale_mk,
                    "caddy_net":      caddy_net,
                    "caddy_sale_thb": caddy_thb,
                    "cart_net":       cart_net,
                    "cart_sale_thb":  cart_thb,
                    "caddy_status":   caddy_status,
                    "cart_status":    cart_status,
                })

    return rows


# =============================================================================
# 기존 함수들 (변경 없음)
# =============================================================================

def _inject_blur_before_done_script() -> None:
    doc = "window.parent.document"
    components.html(
        f"""
        <script>
        (function() {{
          var doc = {doc};
          var BLUR_DELAY_MS = 500;
          function attachBlurHandler() {{
            var buttons = doc.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {{
              var btn = buttons[i];
              if (btn.textContent.indexOf('수정 완료') !== -1 && !btn.dataset.blurHandled) {{
                btn.dataset.blurHandled = '1';
                btn.addEventListener('mousedown', function(e) {{
                  var target = e.currentTarget;
                  var active = doc.activeElement;
                  if (active && active !== doc.body && active !== target) {{
                    e.preventDefault();
                    e.stopPropagation();
                    try {{
                      active.dispatchEvent(new Event('change', {{ bubbles: true }}));
                      active.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }} catch (err) {{}}
                    active.blur();
                    setTimeout(function() {{ target.click(); }}, BLUR_DELAY_MS);
                  }}
                }}, true);
                return true;
              }}
            }}
            return false;
          }}
          function init() {{
            if (!attachBlurHandler()) {{
              setTimeout(init, 100);
            }}
          }}
          if (doc.readyState === 'loading') {{
            doc.addEventListener('DOMContentLoaded', function() {{ setTimeout(init, 100); }});
          }} else {{
            setTimeout(init, 150);
          }}
        }})();
        </script>
        """,
        height=0,
    )


def export_df_to_google_sheets(
    df: pd.DataFrame,
    spreadsheet_id: str,
    sheet_name: str = "요금표",
    product_name: str = "",
    period: str = "",
    extracted_date: str | None = None,
):
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
    df_str = df.astype(str)
    rows = []
    for i in range(len(df_str)):
        row = [product_name, "", period] + df_str.iloc[i].tolist()
        if len(row) <= 22:
            row.extend([""] * (23 - len(row)))
        row[22] = extracted_date
        rows.append(row)

    if not rows:
        return True

    needed_rows = next_row + len(rows) - 1
    if worksheet.row_count < needed_rows:
        worksheet.add_rows(needed_rows - worksheet.row_count)

    start_cell = f"A{next_row}"
    worksheet.update(range_name=start_cell, values=rows, value_input_option="USER_ENTERED")
    return True


def _parse_period_end(period_str: str):
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
    triple_exchange_rate: float = 0.0,
):
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
    is_triple_sheet = (sheet_name or "").strip() == "트리플 골프"
    if is_triple_sheet:
        triple_header = [
            "상품명", "지역", "기간", "홀", "시간대", "주중/주말",
            "그린피(넷, ฿)", "그린피(세일, ฿)", "캐디피(넷, ฿)", "카트피(넷, ฿)",
            "캐디 포함", "카트 포함", "몽키 넷(฿)", "몽키 세일(฿)",
            "트리플 세일(฿)", "최소 마진 세일(฿)", "트리플 공급가(₩)",
            "몽키 넷(₩)", "마진(₩)", "추출날짜", "몽키 환율", "트리플 환율",
        ]
        worksheet.update(range_name="A1:V1", values=[triple_header], value_input_option="USER_ENTERED")
    else:
        if len(header_row) < 1 or (header_row[0] or "").strip() != "상품명":
            worksheet.update_acell("A1", "상품명")
        if len(header_row) < 3 or (header_row[2] or "").strip() != "기간":
            worksheet.update_acell("C1", "기간")

    all_vals = worksheet.get_all_values()
    next_row = len(all_vals) + 1 if all_vals else 2
    extracted_date = extracted_date or datetime.date.today().isoformat()

    valid_results = []
    for res in results or []:
        df = (res or {}).get("df")
        if df is None or df.empty:
            continue
        valid_results.append(res)

    if is_triple_sheet:
        rows = []
        dark_sep_rows = []
        light_sep_rows = []
        first_block = True
        num_triple_cols = len(triple_header)

        for res in valid_results:
            df = (res or {}).get("df")
            if df is None or df.empty:
                continue
            meta = (res or {}).get("meta") or {}
            pname_dict = meta.get("product_name") or {}
            product_name = pname_dict.get("ko") or pname_dict.get("en") or ""
            period = meta.get("period") or ""
            region = meta.get("city_ko") or ""

            sep_row = [""] * num_triple_cols
            sep_row_index = next_row + len(rows)
            rows.append(sep_row)
            if first_block:
                dark_sep_rows.append(sep_row_index)
                first_block = False
            else:
                light_sep_rows.append(sep_row_index)

            for i in range(len(df)):
                def _cell(col_name: str):
                    if col_name in df.columns:
                        v = df.iloc[i][col_name]
                        return "" if v is None else v
                    return ""

                row = [
                    product_name, region, period,
                    _cell("홀"), _cell("시간대"), _cell("주중/주말"),
                    _cell("그린피(넷, ฿)"), _cell("그린피(세일, ฿)"),
                    _cell("캐디피(넷, ฿)"), _cell("카트피(넷, ฿)"),
                    _cell("캐디 포함"), _cell("카트 포함"),
                    _cell("몽키 넷(฿)"), _cell("몽키 세일(฿)"),
                    _cell("트리플 세일(฿)"), _cell("최소 마진 세일(฿)"),
                    _cell("트리플 공급가(₩)"), _cell("몽키 넷(₩)"), _cell("마진(₩)"),
                    extracted_date,
                    exchange_rate if exchange_rate else "",
                    triple_exchange_rate if triple_exchange_rate else "",
                ]
                rows.append([("" if v is None else str(v)) for v in row])

        if not rows:
            return True

        needed_rows = next_row + len(rows) - 1
        if worksheet.row_count < needed_rows:
            worksheet.add_rows(needed_rows - worksheet.row_count)

        worksheet.update(range_name=f"A{next_row}", values=rows, value_input_option="USER_ENTERED")

        def _format_triple_sep_rows(idxs, bg):
            if not idxs:
                return
            for r in idxs:
                worksheet.format(f"A{r}:V{r}", {
                    "backgroundColor": {"red": bg[0], "green": bg[1], "blue": bg[2]},
                    "textFormat": {"bold": True},
                })

        _format_triple_sep_rows(dark_sep_rows, (0.4, 0.4, 0.4))
        _format_triple_sep_rows(light_sep_rows, (0.9, 0.9, 0.9))
        return True

    increase_rate_map = {}
    sale_col = None
    num_export_cols = 24

    if len(valid_results) >= 2:
        res_old, res_new = valid_results[-2], valid_results[-1]
        df_old, df_new = res_old.get("df"), res_new.get("df")
        for c in df_new.columns:
            if str(c).startswith("판매가_"):
                sale_col = c
                break
        adj_col = None
        if sale_col:
            suffix = str(sale_col).replace("판매가_", "", 1)
            adj_col = f"최종판매가_{suffix}"
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
        num_export_cols = 25

    rows = []
    dark_sep_rows = []
    light_sep_rows = []
    first_block = True

    for res in results or []:
        df = (res or {}).get("df")
        if df is None or df.empty:
            continue
        meta = (res or {}).get("meta") or {}
        pname_dict = meta.get("product_name") or {}
        product_name = pname_dict.get("ko") or pname_dict.get("en") or ""
        period = meta.get("period") or ""
        region = meta.get("city_ko") or ""

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
        is_newest_block = len(valid_results) >= 2 and res is valid_results[-1]

        for i in range(len(df_str)):
            row = [product_name, region, period] + df_str.iloc[i].tolist()
            if len(row) <= 22:
                row.extend([""] * (23 - len(row)))
            row[22] = extracted_date
            row.append(str(exchange_rate) if exchange_rate else "")
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
            worksheet.format(f"A{r}:{last_col}{r}", {
                "backgroundColor": {"red": bg[0], "green": bg[1], "blue": bg[2]},
                "textFormat": {"bold": True},
            })

    _format_rows(dark_sep_rows, (0.4, 0.4, 0.4))
    _format_rows(light_sep_rows, (0.9, 0.9, 0.9))
    return True


# ─────────────────────────────────────────────
#  HTML 파서: 골프 요금표
# ─────────────────────────────────────────────
def parse_golf_html(html: str):
    rows = []
    hole_starts = list(re.finditer(
        r'name="golf_rate\.rateJson\.renderRow\.([^"]+)"',
        html
    ))

    for i, m in enumerate(hole_starts):
        hole = m.group(1)
        block_start = html.rfind('<tr', 0, m.start())
        block_end = hole_starts[i + 1].start() if i + 1 < len(hole_starts) else len(html)
        if i + 1 < len(hole_starts):
            block_end = html.rfind('<tr', 0, hole_starts[i + 1].start())
        block = html[block_start:block_end]

        caddy_net = _extract_val(block, rf'name="golf_rate\.rateJson\.caddy\.{re.escape(hole)}\.nett"')
        caddy_sale_thb = _extract_val(block, rf'name="golf_rate\.rateJson\.caddy\.{re.escape(hole)}\.sale\.THB"')
        cart_net = _extract_val(block, rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(hole)}\.nett"')
        cart_sale_thb = _extract_val(block, rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(hole)}\.sale\.THB"')

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
    pat = name_pattern + r'[^>]*value="([\d,]+)"'
    m = re.search(pat, html_block)
    if m:
        return int(m.group(1).replace(',', ''))
    pat2 = r'value="([\d,]+)"[^>]*' + name_pattern
    m2 = re.search(pat2, html_block)
    if m2:
        return int(m2.group(1).replace(',', ''))
    return None


# ─────────────────────────────────────────────
#  HTML 파서: 기간 / 프로모션 메타
# ─────────────────────────────────────────────
def _extract_attr_value(html: str, name_candidates):
    for key in name_candidates:
        key_esc = re.escape(key)
        m = re.search(rf'(<input[^>]+(?:name|id)="{key_esc}"[^>]*>)', html, flags=re.I)
        if m:
            tag = m.group(1)
            vm = re.search(r'value="([^"]*)"', tag, flags=re.I)
            if vm:
                v = vm.group(1).strip()
                if v:
                    return v
        tm = re.search(rf'<textarea[^>]+(?:name|id)="{key_esc}"[^>]*>([\s\S]*?)</textarea>', html, flags=re.I)
        if tm:
            v = re.sub(r'\s+', ' ', tm.group(1)).strip()
            if v:
                return v
    return None


def parse_promotion_meta(html: str):
    def _date_only(v: str | None):
        if not v:
            return None
        m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", v)
        return m.group(1) if m else v.strip()

    start = _extract_attr_value(html, ["golf_rate.startDate"])
    end = _extract_attr_value(html, ["golf_rate.endDate"])
    promo_name_en = _extract_attr_value(html, ["golf_rate.promotionName_en"])
    promo_name_ko = _extract_attr_value(html, ["golf_rate.promotionName_ko"])
    promo_info_en = _extract_attr_value(html, ["golf_rate.promotionInfo_en"])
    promo_info_ko = _extract_attr_value(html, ["golf_rate.promotionInfo_ko"])

    supplier_match = re.search(r'Supplier\s*:\s*([^<]+)', html, re.IGNORECASE)
    supplier_name = supplier_match.group(1).strip() if supplier_match else None

    promo_name_fallback = _extract_attr_value(html, ["promotionName", "promotion_name", "promoName", "promo_name", "promotionTitle", "promotion_title", "title"])
    promo_info_fallback = _extract_attr_value(html, ["promotionInfo", "promotion_info", "promoInfo", "promo_info", "promotionDesc", "promotion_desc", "description", "desc", "info", "memo", "note", "notes"])

    start = start or _extract_attr_value(html, ["startDate", "start_date", "fromDate", "from_date", "dateFrom", "date_from", "validFrom", "valid_from", "periodFrom", "period_from"])
    end = end or _extract_attr_value(html, ["endDate", "end_date", "toDate", "to_date", "dateTo", "date_to", "validTo", "valid_to", "periodTo", "period_to"])

    if not start or not end:
        dates = re.findall(r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", html)
        if len(dates) >= 2:
            start = start or dates[0]
            end = end or dates[1]

    start_d = _date_only(start)
    end_d = _date_only(end)
    period = f"{start_d} ~ {end_d}" if start_d and end_d else None

    def _pack_lang(en, ko, fallback):
        return {"en": (en or "").strip() or None, "ko": (ko or "").strip() or None, "raw": (fallback or "").strip() or None}

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
    pat = name_pattern + r'[\s\S]*?</select>'
    m = re.search(pat, html_block)
    if not m:
        return None
    select_html = m.group(0)
    sel = re.search(r'selected[^>]*>([^<]+)<', select_html)
    if sel:
        return sel.group(1).strip()
    return None


def build_table(
    rows,
    exchange_rate,
    commission_rates,
    discount_rate,
    min_margin_rate=0.0,
    is_marit: bool = False,
    mode: str = "",
    triple_sale_coef: float = 0.89,
    triple_exchange_rate: float = 0.0,
):
    records = []

    for r in rows:
        hole = r['hole']
        time_of_day = r['time_of_day']
        if is_marit:
            if str(hole).strip() not in ("18H", "36H"):
                continue
            if str(time_of_day).strip() not in ("Morning", "Afternoon"):
                continue
        week_div = r['week_div']
        net_thb = r['net_thb']
        sale_thb = r['sale_thb']
        caddy_net = r['caddy_net']
        caddy_sale = r['caddy_sale_thb']
        cart_net = r['cart_net']
        cart_sale = r['cart_sale_thb']

        pkg_net = net_thb + caddy_net + cart_net
        pkg_sale_monkey = sale_thb + caddy_sale + cart_sale
        pkg_sale = pkg_sale_monkey

        caddy_status = r.get('caddy_status')
        cart_status = r.get('cart_status')
        caddy_include = caddy_status in ('Include', 'Compulsory') if caddy_status else False
        cart_include = cart_status in ('Include', 'Compulsory') if cart_status else False

        if mode == "triple":
            base_comm = float(commission_rates[0]) / 100.0 if commission_rates else 0.0
            denom = triple_sale_coef * (1 - base_comm)
            if denom > 0:
                pkg_sale = round(pkg_net / denom)

            pkg_sale_krw = round(pkg_sale * exchange_rate) if exchange_rate > 0 else 0
            pkg_net_krw = round(pkg_net * exchange_rate) if exchange_rate > 0 else 0

            triple_sale_thb = None
            try:
                _base_rate = float(exchange_rate) or 0.0
                _triple_rate = float(triple_exchange_rate) or 0.0
                if _base_rate > 0 and _triple_rate > 0:
                    triple_sale_thb = round(pkg_sale_monkey * _base_rate / _triple_rate)
            except Exception:
                pass

            rec = {
                '홀': hole,
                '시간대': time_of_day,
                '주중/주말': '주중' if week_div == 'weekday' else '주말/연휴',
                '그린피(넷, ฿)': net_thb,
                '그린피(세일, ฿)': sale_thb,
                '캐디피(넷, ฿)': caddy_net,
                '카트피(넷, ฿)': cart_net,
                '캐디 포함': '✅ ' + (caddy_status or '') if caddy_include else (caddy_status or '-'),
                '카트 포함': '✅ ' + (cart_status or '') if cart_include else (cart_status or '-'),
                '몽키 넷(฿)': pkg_net,
                '몽키 세일(฿)': pkg_sale_monkey,
                '트리플 세일(฿)': triple_sale_thb if triple_sale_thb is not None else '-',
                '최소 마진 세일(฿)': pkg_sale,
            }
            if exchange_rate > 0:
                rec['몽키 넷(₩)'] = pkg_net_krw
            if triple_exchange_rate:
                try:
                    a_krw = float(pkg_sale) * float(triple_exchange_rate)
                    b_krw = int(round(a_krw * 0.95))
                except Exception:
                    b_krw = 0
                rec["트리플 공급가(₩)"] = b_krw
                if exchange_rate > 0:
                    rec["마진(₩)"] = b_krw - int(pkg_net_krw)
            records.append(rec)
            continue

        pkg_sale_krw = round(pkg_sale_monkey * exchange_rate) if exchange_rate > 0 else 0
        pkg_net_krw = round(pkg_net * exchange_rate) if exchange_rate > 0 else 0

        rec = {
            '홀': hole,
            '시간대': time_of_day,
            '주중/주말': '주중' if week_div == 'weekday' else '주말/연휴',
            '그린피(넷, ฿)': net_thb,
            '그린피(세일, ฿)': sale_thb,
            '캐디피(넷, ฿)': caddy_net,
            '카트피(넷, ฿)': cart_net,
            '패키지넷(฿)': pkg_net,
            '패키지세일(฿)': pkg_sale_monkey,
            '캐디 포함': '✅ ' + (caddy_status or '') if caddy_include else (caddy_status or '-'),
            '카트 포함': '✅ ' + (cart_status or '') if cart_include else (cart_status or '-'),
        }
        if exchange_rate > 0:
            rec['패키지넷(₩)'] = pkg_net_krw
            rec['패키지세일(₩)'] = pkg_sale_krw

        margin_pct = min_margin_rate / 100.0 if min_margin_rate is not None else 0.0
        for comm in commission_rates:
            comm_d = comm / 100
            comm_str = str(comm).replace('.', '_')
            base_price_krw = pkg_sale_krw
            base_supply_krw = round(base_price_krw * (1 - comm_d)) if exchange_rate > 0 else 0
            base_margin_krw = base_supply_krw - pkg_net_krw

            need_adjust = (
                (base_margin_krw <= 0)
                and exchange_rate > 0
                and (1 - comm_d) > 0
                and margin_pct < 1.0
            )
            if need_adjust:
                final_supply_krw = math.ceil(pkg_net_krw / (1 - margin_pct))
                final_price_krw = math.ceil(final_supply_krw / (1 - comm_d))
                final_margin_krw = final_supply_krw - pkg_net_krw
            else:
                final_price_krw = base_price_krw
                final_supply_krw = base_supply_krw
                final_margin_krw = base_margin_krw

            if exchange_rate > 0:
                rec[f'판매가_{comm_str}%(₩)'] = base_price_krw
                rec[f'공급가_{comm_str}%(₩)'] = base_supply_krw
                rec[f'마진_{comm_str}%(₩)'] = base_margin_krw
                rec[f'최종판매가_{comm_str}%(₩)'] = final_price_krw
                rec[f'최종공급가_{comm_str}%(₩)'] = final_supply_krw
                rec[f'최종마진_{comm_str}%(₩)'] = final_margin_krw

        records.append(rec)

    return pd.DataFrame(records)


# ─────────────────────────────────────────────
#  요금표 편집 후 공급가/마진 재계산
# ─────────────────────────────────────────────
def _comm_from_col_name(col_name: str, prefix: str) -> float | None:
    if not col_name.startswith(prefix) or "%(₩)" not in col_name:
        return None
    try:
        mid = col_name[len(prefix) : col_name.index("%(₩)")]
        return float(mid.replace("_", ".", 1))
    except (ValueError, TypeError):
        return None


def apply_price_edits(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "최소 마진 세일(฿)" in out.columns and "몽키 넷(₩)" in out.columns:
        pkg_net = out["몽키 넷(₩)"].astype(float, errors="ignore").fillna(0)
        try:
            triple_rate = float(st.session_state.get("triple_exchange_rate", 45.0)) or 0.0
        except Exception:
            triple_rate = 0.0
        if triple_rate > 0:
            pkg_sale_thb = pd.to_numeric(out["최소 마진 세일(฿)"], errors="coerce").fillna(0)
            a_krw = (pkg_sale_thb * triple_rate).round().astype(int)
            b_krw = (a_krw * 0.95).round().astype(int)
            if "트리플 공급가(₩)" in out.columns:
                out["트리플 공급가(₩)"] = b_krw
        if "마진(₩)" in out.columns and "트리플 공급가(₩)" in out.columns:
            triple_supply = pd.to_numeric(out["트리플 공급가(₩)"], errors="coerce").fillna(0)
            out["마진(₩)"] = (triple_supply - pkg_net).round().astype(int)
        return out

    if "패키지넷(₩)" not in out.columns:
        return out
    pkg_net = out["패키지넷(₩)"].astype(float, errors="ignore").fillna(0)

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

    try:
        margin_pct = float(st.session_state.get("min_margin_rate", 0.0)) / 100.0
    except Exception:
        margin_pct = 0.0
    for col in list(out.columns):
        if not col.startswith("판매가_") or "(₩)" not in col:
            continue
        rate = _comm_from_col_name(col, "판매가_")
        if rate is None:
            continue
        suffix = col.replace("판매가_", "", 1)
        supply_col = f"공급가_{suffix}"
        margin_col = f"마진_{suffix}"
        final_sale_col = f"최종판매가_{suffix}"
        final_supply_col = f"최종공급가_{suffix}"
        final_margin_col = f"최종마진_{suffix}"
        if not all(c in out.columns for c in [supply_col, margin_col, final_sale_col, final_supply_col, final_margin_col]):
            continue
        base_sale = pd.to_numeric(out[col], errors="coerce").fillna(0)
        base_supply = pd.to_numeric(out[supply_col], errors="coerce").fillna(0)
        base_margin = pd.to_numeric(out[margin_col], errors="coerce").fillna(0)

        final_sale = base_sale.copy()
        final_supply = base_supply.copy()
        final_margin = base_margin.copy()

        if margin_pct > 0.0 and margin_pct < 1.0:
            mask = base_margin <= 0
            if mask.any():
                target_supply = (pkg_net / (1 - margin_pct)).apply(lambda x: int(math.ceil(x)) if pd.notna(x) else 0)
                target_sale = (target_supply / (1 - rate / 100.0)).apply(lambda x: int(math.ceil(x)) if pd.notna(x) else 0)
                target_margin = target_supply - pkg_net
                final_sale[mask] = target_sale[mask]
                final_supply[mask] = target_supply[mask]
                final_margin[mask] = target_margin[mask]

        out[final_sale_col] = final_sale.astype(int)
        out[final_supply_col] = final_supply.astype(int)
        out[final_margin_col] = final_margin.astype(int)

    return out


# ─────────────────────────────────────────────
#  스타일
# ─────────────────────────────────────────────
BOLD_PREFIXES = ('판매가_', '공급가_', '마진_', '최종판매가_', '최종공급가_', '최종마진_')

def style_df(df):
    bold_cols = {
        i for i, col in enumerate(df.columns)
        if col.startswith(BOLD_PREFIXES) or col == "최소 마진 세일(฿)" or col == "마진(₩)"
    }
    sale_cols = {i for i, col in enumerate(df.columns) if (col.startswith("판매가_") or col.startswith("최종판매가_")) and "(₩)" in col}
    margin_cols = {
        i for i, col in enumerate(df.columns)
        if ((col.startswith("마진_") or col.startswith("최종마진_")) and "(₩)" in col) or col == "마진(₩)"
    }
    divider_col = None
    for i, col in enumerate(df.columns):
        if col == "트리플 공급가(₩)":
            divider_col = i
            break

    def highlight(row):
        styles = [''] * len(row)
        for i in range(len(row)):
            col = row.index[i]
            parts = []
            if i in bold_cols:
                parts.append('font-weight: bold')
            if i in sale_cols:
                parts.append('background-color: #dbeafe')
            if i in margin_cols:
                try:
                    v = float(str(row[col]).replace(',', '').replace('원', ''))
                    if v < 0:
                        parts.append('background-color: #fee2e2')
                        parts.append('color: #dc2626')
                    else:
                        parts.append('background-color: #fef3c7')
                except Exception:
                    parts.append('background-color: #fef3c7')
            if divider_col is not None and i == divider_col:
                parts.append('border-right: 3px solid #000')
            if parts:
                styles[i] = '; '.join(parts)
        return styles

    return df.style.apply(highlight, axis=1)


# =============================================================================
# 검색 탭 UI
# =============================================================================

def search_tab_ui():
    # session_state 초기화
    for k, v in [
        ("stab_products",  []),
        ("stab_selected",  None),
        ("stab_rate_hits", []),
        ("stab_built",     {}),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v

    st.markdown("### 🔍 골프 상품 검색")
    st.caption("상품명(한/영) 또는 별칭으로 검색합니다. 예: 니칸티, Nikanti, 방푸, Alpine")

    col_kw, col_btn, _ = st.columns([3, 1, 2])
    with col_kw:
        keyword = st.text_input(
            "상품명",
            placeholder="예: 니칸티, 알파인, 레드마운틴",
            key="stab_kw_input",
            label_visibility="collapsed",
        )
    with col_btn:
        do_search = st.button("검색", use_container_width=True, type="primary", key="stab_search_btn")

    if do_search:
        if not keyword.strip():
            st.warning("상품명을 입력해 주세요.")
            return
        with st.spinner("검색 중..."):
            products = search_golf_products(keyword.strip())
        st.session_state["stab_products"]  = products
        st.session_state["stab_selected"]  = None
        st.session_state["stab_rate_hits"] = []
        st.session_state["stab_built"]     = {}

    products = st.session_state["stab_products"]
    if not products:
        if do_search:
            st.info("검색 결과가 없습니다.")
        return

    st.markdown(f"**{len(products)}개 상품 검색됨**")

    def _label(p):
        return f"{p['name_ko']}  /  {p['name_en']}  (ID: {p['product_id']})"

    product_map = {_label(p): p for p in products}
    # '상품명' 입력칸(좌측 3/6) 폭과 동일하게 맞춥니다.
    col_sel, _, _ = st.columns([3, 1, 2])
    with col_sel:
        sel_label = st.selectbox("상품", options=list(product_map.keys()), key="stab_product_sel", label_visibility="collapsed")
    selected = product_map[sel_label]

    if st.session_state["stab_selected"] != selected:
        st.session_state["stab_selected"]  = selected
        st.session_state["stab_rate_hits"] = []
        st.session_state["stab_built"]     = {}

    if not st.session_state["stab_rate_hits"]:
        with st.spinner("요금 데이터 로딩 중..."):
            hits = fetch_golf_rates_by_product(selected["product_id"])
        st.session_state["stab_rate_hits"] = hits
        st.session_state["stab_built"]     = {}

    hits = st.session_state["stab_rate_hits"]
    if not hits:
        st.info("유효한 요금 기간이 없습니다. (만료 또는 isUse=0)")
        return

    def _period_label(r):
        start = r.get("startDate") or ""
        end   = r.get("endDate")   or ""
        promo = r.get("promotionName_ko") or r.get("promotionName_en") or ""
        return f"{start} ~ {end}  [{promo}]" if promo else f"{start} ~ {end}"

    period_options = [_period_label(r) for r in hits]
    period_map     = {_period_label(r): r for r in hits}

    st.markdown("---")
    # `main()` 상단의 플랫폼 라디오에서 선택한 값을 사용합니다.
    sel_mode = st.session_state.get("stab_mode", GOLF_MODES[0][1])
    mode_val_now, _ = _golf_mode_from_label(sel_mode)
    is_mrt_now = (mode_val_now == "mrt")

    # 환율/수수료 입력칸 너비를 "기간 입력칸"과 같은 비율로 맞추되,
    # 요청대로 `수수료`가 `환율` 바로 오른쪽에 오도록 배치합니다.
    # - mrt: 기간1/기간2가 `st.columns([1,1,1,1])` 기준 1/4 폭이므로, 환율/수수료도 각각 1/4 폭으로 놓고 둘을 인접 배치합니다.
    # - 그 외: 기존처럼 2분할(각 1/2 폭) 유지합니다.
    if is_mrt_now:
        col_exr, col_comm, _, _ = st.columns([1, 1, 1, 1])
    else:
        # 비-mrt(카카오/트리플)는 기간 1개 폭과 같은 스케일로 맞추기 위해
        # 환율/수수료를 각각 1/4 폭(합 1/2)으로 배치하고 나머지는 여백 처리합니다.
        col_exr, col_comm, _ = st.columns([1, 1, 2])

    with col_exr:
        exr_str = st.text_input("환율 (THB→KRW)", value="48.5", key="stab_exr")
    with col_comm:
        comm_str = st.text_input("수수료 (%)", placeholder="예: 4,6.6,10", key="stab_comm")

    triple_coef = 0.89
    triple_exr  = 0.0
    if sel_mode == "트리플 골프":
        # 환율/수수료 입력칸과 동일한 가로 사이즈에 맞춥니다.
        # (search_tab_ui에서 비-mrt의 환율/수수료는 st.columns([1,1,2]) 기준으로 각각 1/4 폭)
        c_coef, c_texr, _ = st.columns([1, 1, 2])
        with c_coef:
            triple_coef = st.number_input("최소 마진 계수", min_value=0.01, max_value=1.0, value=0.89, step=0.01, format="%.2f", key="stab_coef")
        with c_texr:
            triple_exr = st.number_input("트리플 환율", min_value=0.0, value=45.0, step=0.1, format="%.1f", key="stab_texr")

    # ── 기간 선택 (마리트는 2개까지, 나머지는 1개)

    calc_clicked = False
    src_list = []
    if is_mrt_now:
        st.caption("마이리얼트립은 기간을 2개까지 선택할 수 있습니다. 2개 선택 시 구글 시트에 증감률이 추가됩니다.")

        # 기간 1/기간 2/마크업 계산을 한 줄(columns)로 배치합니다.
        # "검색" 버튼과 동일한 가로 사이즈(전체 폭의 1/6)는 `마크업 계산` 버튼에,
        # 기간 1/기간 2 입력칸 폭은 기존 값(감소된 상태)으로 유지합니다.
        # (기간1/기간2 = 1/4, 버튼 = 1/6, 남는 공간 = 나머지)
        p1_col, p2_col, btn_col, _ = st.columns([3, 3, 2, 4])
        with p1_col:
            sel_period1 = st.selectbox("기간 1", options=period_options, key="stab_period_sel1")
        with p2_col:
            sel_period2 = st.selectbox(
                "기간 2",
                options=period_options,
                index=min(1, len(period_options) - 1),
                key="stab_period_sel2",
            )
        with btn_col:
            calc_clicked = st.button(
                "🔢 마크업 계산",
                type="primary",
                key="stab_calc",
                use_container_width=True,
            )

        # 체크박스는 버튼 아래로 둬서, 요청하신 "한 줄" 배치를 방해하지 않도록 합니다.
        use_two = st.checkbox("기간 2개 비교 (증감률 계산)", value=len(period_options) > 1, key="stab_use_two")
        src_list = [period_map[sel_period1]]
        if use_two and sel_period2 != sel_period1:
            src_list.append(period_map[sel_period2])
        elif use_two and sel_period2 == sel_period1:
            st.warning("기간 1과 기간 2가 같습니다. 다른 기간을 선택해 주세요.")
            src_list = [period_map[sel_period1]]
    else:
        # 비-mrt는 기간 selectbox와 버튼을 한 줄로 배치합니다.
        # 버튼 폭은 "검색" 버튼과 동일하게(전체 폭의 1/6) 유지하고,
        # 기간 selectbox 폭만 더 줄입니다.
        p_col, btn_col, _ = st.columns([2, 1, 3])
        with p_col:
            sel_period1 = st.selectbox("기간", options=period_options, key="stab_period_sel1")
            src_list = [period_map[sel_period1]]
        with btn_col:
            calc_clicked = st.button(
                "🔢 마크업 계산",
                type="primary",
                key="stab_calc",
                use_container_width=True,
            )

    gid_key = "_".join(str(s.get("id") or "") for s in src_list)

    if calc_clicked:
        try:
            exchange_rate = float(exr_str.strip()) if exr_str.strip() else 0.0
        except Exception:
            exchange_rate = 0.0
        if not (comm_str or "").strip():
            st.warning("수수료를 입력해 주세요. (예: 4,6.6,10)")
            commission_rates = []
        else:
            try:
                commission_rates = [float(x.strip()) for x in comm_str.split(",") if x.strip()]
            except Exception:
                commission_rates = []

        mode_val, _ = _golf_mode_from_label(sel_mode)
        built = st.session_state.get("stab_built") or {}

        for idx_s, src in enumerate(src_list):
            gid = str(src.get("id") or "")
            rows = parse_rate_json(src.get("rateJson"))
            if not rows:
                st.error(f"기간 {idx_s+1}: rateJson에서 요금 데이터를 찾을 수 없습니다.")
                continue
            df = build_table(
                rows, exchange_rate, commission_rates, 0.0,
                min_margin_rate=0.0,
                is_marit=(mode_val == "mrt"),
                mode=mode_val,
                triple_sale_coef=triple_coef,
                triple_exchange_rate=triple_exr,
            )
            meta = {
                "period": f"{src.get('startDate') or ''} ~ {src.get('endDate') or ''}",
                "promotion_name": {"en": src.get("promotionName_en") or "", "ko": src.get("promotionName_ko") or ""},
                "promotion_info": {"en": src.get("promotionInfo_en") or "", "ko": src.get("promotionInfo_ko") or ""},
                "product_name":   {"en": selected["name_en"], "ko": selected["name_ko"]},
                "city_ko":         selected.get("city_ko") or "",
            }
            built[gid] = {"df": df, "meta": meta, "rows": rows, "exchange_rate": exchange_rate, "commission_rates": commission_rates}

        st.session_state["stab_built"] = built

    # ── 결과 표시
    built = st.session_state.get("stab_built") or {}
    results_to_show = [built[str(s.get("id") or "")] for s in src_list if str(s.get("id") or "") in built]

    if not results_to_show:
        return

    st.markdown("---")
    city_ko = selected.get("city_ko") or ""
    city_badge = f'<span style="background:#e0f2fe; color:#0369a1; border-radius:4px; padding:2px 8px; font-size:0.85rem; margin-left:8px;">{city_ko}</span>' if city_ko else ""
    st.markdown(
        f'<div style="margin-bottom:0.8rem;">'
        f'<div style="font-size:1.6rem; font-weight:700;">{selected["name_ko"]}{city_badge}</div>'
        f'<div style="color:#6b7280; font-size:0.9rem;">{selected["name_en"]}  |  ID: {selected["product_id"]}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 수정 중인 결과 인덱스 초기화
    if "stab_editing_idx" not in st.session_state:
        st.session_state["stab_editing_idx"] = None

    max_fee_rows = max((len(r["df"]) for r in results_to_show if r.get("df") is not None), default=10)
    fee_height        = min(500, 48 + 35 * max_fee_rows)
    fee_editor_height = min(600, 48 + 35 * max_fee_rows)

    for i, res in enumerate(results_to_show):
        # 최신 df 반영 (수정 후 갱신된 것 사용)
        gid_i = str(src_list[i].get("id") or i)
        res   = built.get(gid_i, res)
        df    = res["df"]
        meta  = res["meta"]

        period   = meta.get("period") or "-"
        promo_ko = (meta.get("promotion_name") or {}).get("ko") or "-"
        info_ko  = (meta.get("promotion_info") or {}).get("ko") or ""

        label = f"기간 {i+1}" if len(results_to_show) > 1 else "요금표"
        st.markdown(f"### {label}")
        row_html = (
            f'<div style="display:flex; gap:2rem; margin-bottom:0.8rem; flex-wrap:wrap;">'
            f'<div><div style="color:#6b7280; font-size:0.8rem;">기간</div><div style="font-size:1.1em; font-weight:600;">{period}</div></div>'
            f'<div><div style="color:#6b7280; font-size:0.8rem;">프로모션</div><div style="font-size:1.1em;">{promo_ko}</div></div>'
        )
        if info_ko:
            row_html += f'<div><div style="color:#6b7280; font-size:0.8rem;">비고</div><div style="font-size:1.0em; color:#555;">{info_ko}</div></div>'
        row_html += '</div>'
        st.markdown(row_html, unsafe_allow_html=True)

        is_editing = st.session_state.get("stab_editing_idx") == gid_i
        tit_col, btn_col = st.columns([11, 1])
        with tit_col:
            st.markdown(f"**요금표 ({len(df)}개 항목)**")
        with btn_col:
            if is_editing:
                if st.button("✅ 수정 완료", key=f"stab_done_{gid_key}_{i}"):
                    st.session_state["stab_editing_idx"] = None
                    st.rerun()
            else:
                if st.button("✏️ 수정", key=f"stab_edit_{gid_key}_{i}"):
                    st.session_state["stab_editing_idx"] = gid_i
                    st.rerun()

        if not is_editing:
            st.caption("마진이 마이너스인 셀은 빨간색으로 표시됩니다.")
            display_df = df.copy()
            for c in display_df.columns:
                if any(x in c for x in ['฿)', '(฿', '₩)', '(₩']) or c == "트리플 공급가(₩)":
                    display_df[c] = display_df[c].apply(lambda x: f"{int(round(x)):,}" if isinstance(x, (int, float)) else x)
            st.dataframe(style_df(display_df), use_container_width=True, height=fee_height, hide_index=True)
        else:
            mode_val_i, _ = _golf_mode_from_label(sel_mode)
            if mode_val_i == "triple":
                st.caption("최소 마진 세일(฿)을 수정하면 트리플 공급가(₩)·마진이 자동으로 다시 계산됩니다.")
            else:
                st.caption("판매가를 수정하면 공급가·마진·최종판매가·최종공급가·최종마진이 자동으로 다시 계산됩니다.")
            column_config = {}
            for col in df.columns:
                if col == "최소 마진 세일(฿)":
                    column_config[col] = st.column_config.NumberColumn(col, format="%d")
                elif col.startswith("판매가_") and "(₩)" in col:
                    column_config[col] = st.column_config.NumberColumn(col, format="%d")
                else:
                    column_config[col] = st.column_config.Column(col, disabled=True)

            edited_df = st.data_editor(df, key=f"stab_editor_{gid_key}_{i}", column_config=column_config, use_container_width=True, height=fee_editor_height)
            _inject_blur_before_done_script()

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
                new_built = dict(st.session_state.get("stab_built") or {})
                new_built[gid_i] = {**res, "df": recalc_df}
                st.session_state["stab_built"] = new_built
                st.rerun()

        csv = df.to_csv(index=False, encoding="utf-8-sig")
        st.download_button("📥 CSV 다운로드", data=csv, file_name=f"golf_{selected['product_id']}_{gid_i}.csv", mime="text/csv", key=f"stab_csv_{gid_key}_{i}")

    # ── 구글 시트 내보내기
    has_gcp_secret = bool(st.secrets.get("gcp_service_account"))
    sheet_url = "https://docs.google.com/spreadsheets/d/1qDp5Ty_NnQgYKfyhOnV0l5q7v8TPFtvsIi91GirO180/edit"
    if GSPREAD_AVAILABLE and has_gcp_secret:
        st.markdown("---")
        col_l, col_btn, col_r = st.columns([2, 1, 2])
        with col_btn:
            if st.button("📤 구글 시트로 내보내기", key=f"stab_gsheet_{gid_key}"):
                raw = str(sheet_url).strip()
                sid = raw.split("/d/")[1].split("/")[0].split("?")[0] if "/d/" in raw else raw
                try:
                    _, export_sheet_name = _golf_mode_from_label(sel_mode)
                    exr = results_to_show[0].get("exchange_rate", 0.0) if results_to_show else 0.0
                    with st.spinner("내보내는 중..."):
                        export_all_results_to_google_sheets(
                            results_to_show, sid,
                            sheet_name=export_sheet_name,
                            exchange_rate=exr,
                            triple_exchange_rate=triple_exr,
                        )
                    st.success("구글 시트로 내보냈습니다.")
                except Exception as e:
                    st.error(f"내보내기 실패: {e}")


# =============================================================================
# HTML 입력 탭 UI (기존 main() 로직)
# =============================================================================

def html_tab_ui():
    # session_state 초기화
    for key, default in [
        ('html_key', 0), ('result_df', None),
        ('exchange_rate', 0.0), ('commission_rates', []),
        ('min_margin_rate', 0.0),
        ('triple_sale_coef', 0.89),
        ('triple_exchange_rate', 45.0),
        ('html_blocks', 1),
        ('results', None),
        ('scroll_to_results', False),
        ('editing_fee_idx', None),
        ('golf_mode_radio', GOLF_MODES[0][1]),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    col1, col2 = st.columns(2)
    with col1:
        exchange_input = st.text_input("환율 (THB → KRW)", placeholder="예: 43.5", value="48.5", key="html_exr")
    with col2:
        commission_input = st.text_input("수수료 (%)", placeholder="예: 4,6.6,10", value="", key="html_comm")

    mode_labels = [m[1] for m in GOLF_MODES]
    selected_label = st.radio(
        "플랫폼 유형",
        options=mode_labels,
        horizontal=True,
        help="마이리얼트립: 27H 제외, Night 제외(18H/36H, Morning/Afternoon만). 카카오/트리플: 필터 없음.",
        key="golf_mode_radio",
    )

    if selected_label == "트리플 골프":
        st.info("**최소 마진 패키지세일(฿) = 패키지넷 ÷ (계수 × (1 - 수수료율))**")
        # html_tab_ui에서 환율/수수료는 각각 `st.columns(2)`의 1/2 폭입니다.
        # 트리플 입력칸도 동일한 1/2 폭이 되도록 맞춥니다.
        col_coef, col_triple_rate = st.columns(2)
        with col_coef:
            st.number_input("최소 마진 수식 계수", min_value=0.01, max_value=1.0, value=0.89, step=0.01, format="%.2f", key="triple_sale_coef", help="패키지 세일가(최소 마진 수식) 계산에 사용됩니다.")
        with col_triple_rate:
            st.number_input("트리플 환율 (THB → KRW)", min_value=0.0, value=45.0, step=0.1, format="%.1f", key="triple_exchange_rate", help="트리플 골프 요금표 원화 환산에 사용됩니다.")

    st.markdown("### 골프 요금표 HTML 붙여넣기")
    st.caption("추가 버튼으로 입력칸을 최대 5개까지 추가할 수 있어요.")
    add_clicked = st.button("➕ 추가", use_container_width=False, help="HTML 입력칸 추가 (최대 5개)", key="html_add")
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
        calc_btn = st.button("🔢 계산하기", use_container_width=True, type="primary", key="html_calc")
        if st.button("🗑️ Clear", use_container_width=True, help="입력/결과 전체 초기화", key="html_clear"):
            st.session_state['html_key'] += 1
            st.session_state['result_df'] = None
            st.session_state['results'] = None
            st.session_state['min_margin_rate'] = 0.0
            st.session_state['html_blocks'] = 1
            st.rerun()

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
            if not commission_input.strip():
                st.warning("수수료를 입력해 주세요. (예: 4,6.6,10)")
                commission_rates = []
            else:
                try:
                    commission_rates = [float(x.strip()) for x in commission_input.split(',') if x.strip()]
                except:
                    commission_rates = []
                    st.warning("수수료 형식 오류 → 빈 리스트로 처리")

            min_margin_rate = float(st.session_state.get("min_margin_rate", 0.0)) or 0.0
            _mode_val, _ = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
            is_marit = _mode_val == "mrt"
            results = []
            total_rows = 0
            for idx, html_input in enumerate(valid_htmls, start=1):
                rows = parse_golf_html(html_input)
                meta = parse_promotion_meta(html_input)
                if not rows:
                    results.append({"idx": idx, "error": "HTML에서 골프 요금 데이터를 찾지 못했습니다. 올바른 골프 요금표 HTML인지 확인해 주세요.", "meta": meta, "rows": None, "df": None, "html": html_input})
                    continue

                df = build_table(
                    rows, exchange_rate, commission_rates, 0.0,
                    min_margin_rate=min_margin_rate if _mode_val != "triple" else 0.0,
                    is_marit=is_marit, mode=_mode_val,
                    triple_sale_coef=st.session_state.get("triple_sale_coef", 0.89),
                    triple_exchange_rate=float(st.session_state.get("triple_exchange_rate", 45.0)) or 0.0,
                )
                total_rows += len(rows)
                results.append({"idx": idx, "error": None, "meta": meta, "rows": rows, "df": df, "html": html_input})

            st.session_state['results'] = results
            st.session_state['result_df'] = None
            st.session_state['exchange_rate'] = exchange_rate
            st.session_state['commission_rates'] = commission_rates
            st.session_state['min_margin_rate'] = min_margin_rate
            st.session_state['scroll_to_results'] = True
            st.success(f"✅ 총 {len(valid_htmls)}개 HTML 처리 완료! (파싱된 요금 항목 합계: {total_rows}개)")
            st.rerun()

    if st.session_state.get('results'):
        exchange_rate = st.session_state['exchange_rate']
        commission_rates = st.session_state['commission_rates']
        min_margin_rate = st.session_state.get('min_margin_rate', 0.0)

        st.markdown('<div id="result-section"></div>', unsafe_allow_html=True)
        if st.session_state.get('scroll_to_results'):
            st.session_state['scroll_to_results'] = False
            components.html("""
            <script>
            setTimeout(function() {
                var doc = (window.parent && window.parent.document) ? window.parent.document : document;
                var el = doc.getElementById('result-section');
                if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
            }, 300);
            </script>
            """, height=0)
        st.markdown("---")

        first_meta = (st.session_state.get("results") or [{}])[0].get("meta") or {}
        product_name = first_meta.get("product_name") or {}
        pname_en = (product_name.get("en") or "").strip() or "-"
        pname_esc = html.escape(pname_en)
        st.markdown("**상품명**")
        st.markdown(f'<div style="font-size:3rem; margin-bottom:1.5rem;">{pname_esc}</div>', unsafe_allow_html=True)

        _mode_val, _ = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
        display_rate = float(st.session_state.get("triple_exchange_rate", 45.0)) or 0.0 if _mode_val == "triple" else exchange_rate
        rate_label = "환율 (트리플 정산)" if _mode_val == "triple" else "환율"
        c1, c2, c3 = st.columns(3)
        c1.metric(rate_label, f"1 THB = {display_rate:,.2f} KRW" if display_rate > 0 else "미설정")
        c2.metric("수수료", ", ".join([f"{x}%" for x in commission_rates]) if commission_rates else "미설정")
        if _mode_val != "triple":
            c3.metric("목표 마진율", f"{min_margin_rate:.2f}%" if min_margin_rate and min_margin_rate > 0 else "미설정")
        else:
            c3.empty()

        if _mode_val != "triple":
            st.markdown("### 마진 설정")
            col_margin, col_apply = st.columns([2, 1])
            with col_margin:
                margin_input_val = st.text_input(
                    "목표 마진율 (%)",
                    help="마진이 마이너스일 경우에만 적용됩니다.",
                    value=str(st.session_state.get('min_margin_rate', 0.0)) if st.session_state.get('min_margin_rate', 0.0) > 0 else "",
                    placeholder="미입력 시 조정마진 0 (손익분기 기준)",
                    key="margin_rate_input"
                )
            with col_apply:
                st.write("　")
                if st.button("✅ 마진율 적용", use_container_width=True, key="margin_apply"):
                    try:
                        new_margin = float(margin_input_val.strip()) if margin_input_val.strip() else 0.0
                    except Exception:
                        new_margin = 0.0
                    st.session_state['min_margin_rate'] = new_margin
                    new_results = []
                    for r in st.session_state.get('results', []):
                        if r.get("error") or not r.get("rows"):
                            new_results.append(r)
                            continue
                        _m, _ = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
                        df = build_table(r["rows"], st.session_state['exchange_rate'], st.session_state['commission_rates'], 0.0, min_margin_rate=new_margin, is_marit=(_m == "mrt"), mode=_m)
                        new_results.append({**r, "df": df})
                    st.session_state['results'] = new_results
                    st.rerun()

        results_list = st.session_state.get('results') or []
        max_fee_rows = max((len(r["df"]) for r in results_list if r.get("df") is not None), default=10)
        fee_height = min(500, 48 + 35 * max_fee_rows)
        fee_editor_height = min(600, 48 + 35 * max_fee_rows)

        for i, res in enumerate(results_list):
            idx = res.get("idx", i + 1)
            meta = res.get("meta") or {}

            st.markdown("---")
            st.markdown(f"## 결과 #{idx}")

            pn = meta.get("promotion_name") or {}
            pi = meta.get("promotion_info") or {}
            pn_text = " / ".join([x for x in [(pn.get("en") or "-"), (pn.get("ko") or "-")] if x]) if isinstance(pn, dict) else (pn or "-")
            pi_text = " / ".join([x for x in [(pi.get("en") or "-"), (pi.get("ko") or "-")] if x]) if isinstance(pi, dict) else (pi or "-")

            period_val = meta.get("period") or "-"
            st.markdown(
                '<div style="display:flex; gap:2rem; margin-bottom:1rem; flex-wrap:wrap;">'
                f'<div style="flex:1; min-width:120px;"><div style="font-size:1.5rem; color:#6b7280;">기간</div><div style="font-size:1.75em;">{html.escape(period_val)}</div></div>'
                f'<div style="flex:1; min-width:120px;"><div style="font-size:1.5rem; color:#6b7280;">Promotion name</div><div style="font-size:1.75em;">{html.escape(pn_text)}</div></div>'
                f'<div style="flex:1; min-width:120px;"><div style="font-size:1.5rem; color:#6b7280;">Promotion info</div><div style="font-size:1.75em;">{html.escape(pi_text)}</div></div>'
                '</div>',
                unsafe_allow_html=True,
            )

            if res.get("error"):
                st.error(res["error"])
                continue

            df: pd.DataFrame = res["df"]

            is_editing = st.session_state.get("editing_fee_idx") == idx
            if not is_editing:
                st.caption("마진이 마이너스인 셀은 빨간색으로 표시됩니다.")
            tit_col, btn_col = st.columns([11, 1])
            with tit_col:
                st.markdown(f"### 요금표 ({len(df)}개 항목)")
            with btn_col:
                if is_editing:
                    if st.button("✅ 수정 완료", key=f"done_edit_{i}_{idx}"):
                        st.session_state["editing_fee_idx"] = None
                        st.rerun()
                else:
                    if st.button("✏️ 수정", key=f"edit_btn_{i}_{idx}"):
                        st.session_state["editing_fee_idx"] = idx
                        st.rerun()

            if not is_editing:
                display_for_style = df.copy()
                for c in display_for_style.columns:
                    if any(x in c for x in ['฿)', '(฿', '₩)', '(₩']) or c == '패키지세일(B, 최소 마진 수식)' or c == "트리플 공급가(₩)":
                        display_for_style[c] = display_for_style[c].apply(lambda x: f"{int(round(x)):,}" if isinstance(x, (int, float)) else x)
                st.dataframe(style_df(display_for_style), use_container_width=True, height=fee_height, hide_index=True)
            else:
                _mode_val_i, _ = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
                if _mode_val_i == "triple":
                    st.caption("최소 마진 세일(฿)을 수정하면 트리플 공급가(₩)·마진이 자동으로 다시 계산됩니다.")
                else:
                    st.caption("판매가를 수정하면 공급가·마진·최종판매가·최종공급가·최종마진이 자동으로 다시 계산됩니다.")
                column_config = {}
                for col in df.columns:
                    if col == "최소 마진 세일(฿)":
                        column_config[col] = st.column_config.NumberColumn(col, format="%d")
                    elif col.startswith("판매가_") and "(₩)" in col:
                        column_config[col] = st.column_config.NumberColumn(col, format="%d")
                    else:
                        column_config[col] = st.column_config.Column(col, disabled=True)

                edited_df = st.data_editor(df, key=f"fee_editor_{i}_{idx}", column_config=column_config, use_container_width=True, height=fee_editor_height)
                _inject_blur_before_done_script()

                recalc_df = apply_price_edits(edited_df)
                same = False
                try:
                    if recalc_df.shape == df.shape and list(recalc_df.columns) == list(df.columns):
                        same = True
                        for c in df.columns:
                            if pd.api.types.is_numeric_dtype(df[c]) and pd.api.types.is_numeric_dtype(recalc_df[c]):
                                if not pd.to_numeric(df[c], errors="coerce").fillna(0).round(2).equals(pd.to_numeric(recalc_df[c], errors="coerce").fillna(0).round(2)):
                                    same = False
                                    break
                            elif not df[c].astype(str).eq(recalc_df[c].astype(str)).all():
                                same = False
                                break
                except Exception:
                    same = False
                if not same:
                    new_results = list(st.session_state.get("results") or [])
                    if 0 <= i < len(new_results):
                        new_results[i] = {**new_results[i], "df": recalc_df}
                    st.session_state["results"] = new_results
                    st.rerun()

            current_df = st.session_state.get("results") or []
            csv_df = df
            for r in current_df:
                if r.get("idx") == idx and r.get("df") is not None:
                    csv_df = r["df"]
                    break
            st.markdown("---")
            csv = csv_df.to_csv(index=False, encoding='utf-8-sig')
            st.download_button(label="📥 CSV 다운로드", data=csv, file_name=f"golf_markup_result_{idx}.csv", mime="text/csv", key=f"csv_dl_{idx}")

        st.markdown("---")
        st.markdown("### 내보내기")
        has_gcp_secret = bool(st.secrets.get("gcp_service_account"))
        sheet_url = "https://docs.google.com/spreadsheets/d/1qDp5Ty_NnQgYKfyhOnV0l5q7v8TPFtvsIi91GirO180/edit?gid=0#gid=0"
        if GSPREAD_AVAILABLE and has_gcp_secret:
            st.caption("모든 요금표를 한 번에 구글 시트로 내보냅니다.")
            col_l, col_btn, col_r = st.columns([2, 1, 2])
            with col_btn:
                if st.button("📤 전체 요금표 구글 시트로 내보내기", key="gsheet_export"):
                    raw = str(sheet_url).strip()
                    sid = raw.split("/d/")[1].split("/")[0].split("?")[0] if "/d/" in raw else raw
                    try:
                        _, export_sheet_name = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
                        with st.spinner("구글 스프레드시트로 내보내는 중..."):
                            export_all_results_to_google_sheets(
                                st.session_state.get("results") or [], sid,
                                sheet_name=export_sheet_name,
                                exchange_rate=st.session_state.get("exchange_rate", 0.0),
                                triple_exchange_rate=float(st.session_state.get("triple_exchange_rate", 45.0)) or 0.0,
                            )
                        st.success("모든 요금표를 구글 스프레드시트로 내보냈습니다.")
                    except Exception as e:
                        st.error(f"내보내기 실패: {e}")
        else:
            st.caption("구글 시트 내보내기를 사용하려면 gspread 설치 및 서비스계정 시크릿 설정이 필요합니다.")


# =============================================================================
# main
# =============================================================================

def main():
    st.markdown(
        """
        <style>
        div[data-testid="stTitle"] { font-size: 2em !important; }
        /* columns 내 요소들을 버튼/입력 하단 기준으로 맞추려는 시도 */
        div[data-testid="stColumns"] { align-items: flex-end !important; }
        /* Streamlit columns 내부 실제 래퍼에 하단 정렬 적용 */
        div.stHorizontalBlock { align-items: flex-end !important; }
        div.stButton { display: flex !important; align-items: flex-end !important; }
        div[data-testid="stRadio"] label { font-size: 2.1rem; font-weight: 600; }
        div.stButton > button { background-color: #2563eb !important; color: white !important; border-radius: 8px; padding: 0.5rem 1.5rem; font-size: 1rem; font-weight: 600; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("⛳ 골프 요금 마크업 계산기")

    # 제목 바로 밑에 플랫폼 유형을 배치합니다.
    st.radio(
        "플랫폼 유형",
        options=[m[1] for m in GOLF_MODES],
        horizontal=True,
        key="stab_mode",
    )

    tab1, tab2 = st.tabs(["🔍 골프 상품 검색", "📋 HTML 직접 입력"])

    with tab1:
        search_tab_ui()

    with tab2:
        html_tab_ui()


if __name__ == "__main__":
    main()
