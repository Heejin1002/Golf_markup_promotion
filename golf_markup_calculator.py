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

# 상품 유형: (value, 라디오 라벨, 구글 시트 탭 이름)
GOLF_MODES = [
    ("mrt", "마이리얼트립 (27H 제외, Night 제외)", "마리트 골프"),
    ("kakao", "카카오 골프", "카카오 골프"),
    ("triple", "트리플 골프", "트리플 골프"),
]


def _golf_mode_from_label(selected_label: str):
    """선택한 라벨에서 (value, sheet_name) 반환."""
    for value, label, sheet_name in GOLF_MODES:
        if label == selected_label:
            return value, sheet_name
    return GOLF_MODES[0][0], GOLF_MODES[0][2]


def _inject_blur_before_done_script() -> None:
    """수정 완료 버튼 클릭 시 포커스가 셀에 있어도 먼저 blur 후 클릭이 처리되도록 스크립트 주입."""
    doc = "window.parent.document"  # iframe 안이므로 부모(Streamlit 앱) 문서 사용
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

        # (판매가 컬럼을 사용하지 않으므로 증가율 비교 로직은 비활성화)
    increase_rate_map = {}
    num_export_cols = 24  # 기본: A~X (추출날짜 + 환율)

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


def build_table(
    rows,
    exchange_rate,
    commission_rates,
    discount_rate,
    is_marit: bool = False,
    mode: str = "",
    triple_sale_coef: float = 0.89,
    triple_exchange_rate: float = 0.0,
):
    """
    rows: parse_golf_html 결과
    반환: DataFrame
    트리플 모드일 때는 triple_exchange_rate(트리플 정산 환율)를 사용해 보조 지표를 계산하고,
    기본 원화 계산은 입력 환율(exchange_rate)을 그대로 사용합니다.
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
        # 몽키 패키지세일: 다른 상품 유형과 동일하게 HTML에서 온 값 합계
        pkg_sale_monkey = sale_thb + caddy_sale + cart_sale
        # 맞춤/기본 패키지세일: 트리플일 때는 맞춤 수식, 아니면 몽키와 동일
        pkg_sale = pkg_sale_monkey

        # 트리플 골프 모드일 때: 맞춤 패키지세일(맞춤 수식) = 패키지넷 / (계수 * (1 - 수수료))
        # 수수료는 입력된 commission_rates 중 첫 번째 값 사용, 없으면 0%
        if mode == "triple":
            base_comm = float(commission_rates[0]) / 100.0 if commission_rates else 0.0
            denom = triple_sale_coef * (1 - base_comm)
            if denom > 0:
                pkg_sale = round(pkg_net / denom)  # 맞춤 패키지세일(맞춤 수식)

        # 원화 환산 (맞춤/기본 패키지세일 기준, 입력 환율 사용)
        pkg_sale_krw = round(pkg_sale * exchange_rate) if exchange_rate > 0 else 0
        pkg_net_krw = round(pkg_net * exchange_rate) if exchange_rate > 0 else 0

        caddy_status = r.get('caddy_status')
        cart_status = r.get('cart_status')
        caddy_include = caddy_status in ('Include', 'Compulsory') if caddy_status else False
        cart_include = cart_status in ('Include', 'Compulsory') if cart_status else False

        # 트리플 세일(฿): 몽키 세일(฿) × (입력 환율 / 트리플 환율)
        triple_sale_thb = None
        if mode == "triple":
            try:
                _base_rate = float(exchange_rate) or 0.0
            except Exception:
                _base_rate = 0.0
            try:
                _triple_rate = float(triple_exchange_rate) or 0.0
            except Exception:
                _triple_rate = 0.0
            if _base_rate > 0 and _triple_rate > 0:
                triple_sale_thb = round(pkg_sale_monkey * _base_rate / _triple_rate)

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
            '맞춤 세일(฿)' if mode == "triple" else '패키지세일(฿)': pkg_sale,
        }

        # 트리플 모드일 때: B 보조 지표 계산
        # B = (맞춤 세일(฿) × 트리플 환율) × 0.95 (정산 5% 공제)
        if mode == "triple" and triple_exchange_rate:
            try:
                a_krw = float(pkg_sale) * float(triple_exchange_rate)
                b_krw = int(round(a_krw * 0.95))
            except Exception:
                b_krw = 0
            rec["트리플 공급가(₩)"] = b_krw

        # 원화 환산
        if exchange_rate > 0:
            rec['몽키 넷(₩)'] = pkg_net_krw

        # 마진(₩): 트리플 공급가(₩) - 몽키 넷(₩)
        if exchange_rate > 0 and "트리플 공급가(₩)" in rec:
            try:
                rec["마진(₩)"] = int(rec["트리플 공급가(₩)"]) - int(pkg_net_krw)
            except Exception:
                rec["마진(₩)"] = 0

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
    """판매가·맞춤 세일(฿) 변경분을 반영해 트리플 공급가·마진을 재계산합니다.

    - 맞춤 세일(฿) 편집 시: 트리플 공급가(₩) 및 마진(₩) 연쇄 반영
    - 최종판매가_/최종공급가_/최종마진_ 관련 로직은 제거 (컬럼도 생성하지 않음)
    """
    out = df.copy()
    if "몽키 넷(₩)" not in out.columns:
        return out
    pkg_net = out["몽키 넷(₩)"].astype(float, errors="ignore").fillna(0)

    # 0) 맞춤 세일(฿) 편집 시 → 트리플 공급가 갱신
    if "맞춤 세일(฿)" in out.columns:
        try:
            triple_rate = float(st.session_state.get("triple_exchange_rate", 45.0)) or 0.0
        except Exception:
            triple_rate = 0.0
        try:
            base_rate = float(st.session_state.get("exchange_rate", 0.0)) or 0.0
        except Exception:
            base_rate = 0.0
        if triple_rate > 0:
            pkg_sale_thb = pd.to_numeric(out["맞춤 세일(฿)"], errors="coerce").fillna(0)
            # A/B/C: 트리플 정산 환율과 입력 환율을 함께 사용
            a_krw = (pkg_sale_thb * triple_rate).round().astype(int)
            b_krw = (a_krw * 0.95).round().astype(int)
            c_thb = b_krw
            if base_rate > 0:
                c_thb = (b_krw / base_rate).round().astype(int)
            if "트리플 공급가(₩)" in out.columns:
                out["트리플 공급가(₩)"] = b_krw
    # 1) 마진(₩) 재계산: 트리플 공급가(₩) - 몽키 넷(₩)
    if "마진(₩)" in out.columns and "트리플 공급가(₩)" in out.columns:
        triple_supply = pd.to_numeric(out["트리플 공급가(₩)"], errors="coerce").fillna(0)
        out["마진(₩)"] = (triple_supply - pkg_net).round().astype(int)

    # 2단계: 최종판매가_/최종공급가_/최종마진_ 관련 로직은 사용하지 않으므로 제거
    return out


# ─────────────────────────────────────────────
#  스타일
# ─────────────────────────────────────────────
BOLD_PREFIXES = ('마진_',)

def style_df(df):
    # 볼드 처리할 컬럼 인덱스 (맞춤 세일 포함)
    bold_cols = {
        i for i, col in enumerate(df.columns)
        if col.startswith(BOLD_PREFIXES) or col == "맞춤 세일(฿)" or col == "마진(₩)"
    }
    # 마진/최종마진 열 강조 (연한 노랑)
    margin_cols = {
        i
        for i, col in enumerate(df.columns)
        if ((col.startswith("마진_") or col.startswith("최종마진_")) and "(₩)" in col) or col == "마진(₩)"
    }

    # 트리플 공급가(₩) 오른쪽에 굵은 구분선 추가
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
            # 지정 컬럼 굵게
            if i in bold_cols:
                parts.append('font-weight: bold')
            # 마진 열 배경 강조 (마이너스면 빨강으로 덮음)
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
            # 트리플 공급가(₩) 바로 오른쪽에 굵은 구분선
            if divider_col is not None and i == divider_col:
                parts.append('border-right: 3px solid #000')
            if parts:
                styles[i] = '; '.join(parts)
        return styles

    return df.style.apply(highlight, axis=1)


# ─────────────────────────────────────────────
#  UI
# ─────────────────────────────────────────────
def main():
    st.title("⛳ 골프 요금 마크업 계산기")
    st.markdown("골프 요금표 HTML을 붙여넣으면 패키지 요금 + 환율 + 수수료를 자동 계산합니다.")

    # 라디오·버튼 스타일
    st.markdown(
        """
        <style>
        div[data-testid="stRadio"] label {
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
        # ('min_margin_rate', 0.0),  # 목표 마진율 기능 제거
        ('triple_sale_coef', 0.89),  # 트리플 골프 맞춤 수식 계수 (패키지세일 = 패키지넷 / (계수 * (1 - 수수료)))
        ('triple_exchange_rate', 45.0),  # 트리플 골프 전용 환율 (THB → KRW)
        ('html_blocks', 1),
        ('results', None),
        ('scroll_to_results', False),
        ('editing_fee_idx', None),  # 수정 중인 요금표 결과 번호 (None이면 미리보기만)
        ('golf_mode_radio', GOLF_MODES[0][1]),  # 상품 유형 라디오 선택값(라벨)
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── 파라미터 입력 (상단)
    col1, col2 = st.columns(2)
    with col1:
        exchange_input = st.text_input("환율 (THB → KRW)", placeholder="예: 43.5", value="48.5")
    with col2:
        commission_input = st.text_input("수수료 (%)", placeholder="예: 4,6.6,10", value="")

    # 상품 유형 선택 (라디오)
    mode_labels = [m[1] for m in GOLF_MODES]
    selected_label = st.radio(
        "상품 유형",
        options=mode_labels,
        horizontal=True,
        help="마이리얼트립: 27H 제외, Night 제외(18H/36H, Morning/Afternoon만). 카카오/트리플: 필터 없음.",
        key="golf_mode_radio",
    )

    # 트리플 골프 선택 시: 맞춤 수식 계수 입력 및 수식 안내
    if selected_label == "트리플 골프":
        st.info("**맞춤 패키지세일(฿) = 패키지넷 ÷ (계수 × (1 - 수수료율))**")
        col_coef, col_triple_rate, _ = st.columns([1, 1, 4])
        with col_coef:
            st.number_input(
                "맞춤 수식 계수",
                min_value=0.01,
                max_value=1.0,
                value=0.89,
                step=0.01,
                format="%.2f",
                key="triple_sale_coef",
                help="패키지 세일가(맞춤 수식) 계산에 사용됩니다.",
            )
        with col_triple_rate:
            st.number_input(
                "트리플 환율 (THB → KRW)",
                min_value=0.0,
                value=45.0,
                step=0.1,
                format="%.1f",
                key="triple_exchange_rate",
                help="트리플 골프 요금표 원화 환산에 사용됩니다.",
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
        # HTML 입력칸 높이에 맞춰 약간 아래에 배치
        st.write("")
        st.write("")
        # 계산하기 버튼 (Clear 바로 위, 같은 폭, 간격은 한 줄만)
        calc_btn = st.button("🔢 계산하기", use_container_width=True, type="primary")
        if st.button("🗑️ Clear", use_container_width=True, help="입력/결과 전체 초기화"):
            st.session_state['html_key'] += 1
            st.session_state['result_df'] = None
            st.session_state['results'] = None
            # st.session_state['min_margin_rate'] = 0.0  # 목표 마진율 기능 제거
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
            _mode_val, _ = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
            is_marit = _mode_val == "mrt"
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

                df = build_table(
                    rows,
                    exchange_rate,
                    commission_rates,
                    0.0,
                    is_marit=is_marit,
                    mode=_mode_val,
                    triple_sale_coef=st.session_state.get("triple_sale_coef", 0.89),
                    triple_exchange_rate=float(st.session_state.get("triple_exchange_rate", 45.0)) or 0.0,
                )
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
            # st.session_state['min_margin_rate'] = min_margin_rate  # 목표 마진율 기능 제거
            st.session_state['scroll_to_results'] = True
            st.success(f"✅ 총 {len(valid_htmls)}개 HTML 처리 완료! (파싱된 요금 항목 합계: {total_rows}개)")
            st.rerun()

    # ── 결과 표시
    if st.session_state.get('results'):
        exchange_rate = st.session_state['exchange_rate']
        commission_rates = st.session_state['commission_rates']
        min_margin_rate = 0.0

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

        # 설정 요약 (공통, 트리플일 때는 트리플 정산 환율 표시)
        _mode_val, _ = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
        display_rate = float(st.session_state.get("triple_exchange_rate", 45.0)) or 0.0 if _mode_val == "triple" else exchange_rate
        rate_label = "환율 (트리플 정산)" if _mode_val == "triple" else "환율"
        c1, c2, c3 = st.columns(3)
        c1.metric(rate_label, f"1 THB = {display_rate:,.2f} KRW" if display_rate > 0 else "미설정")
        c2.metric("수수료", ", ".join([f"{x}%" for x in commission_rates]) if commission_rates else "미설정")
        c3.empty()

        # ── 결과별 표시 (i: 리스트 순서, idx: 결과 번호 — 아래 요금표도 고유 키/갱신 보장)
        results_list = st.session_state.get('results') or []
        # 위·아래 요금표 표현 형식 통일: 모든 요금표에 동일 높이 적용
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
            # 기간 / Promotion name / Promotion info 를 가로 배치 (각각 타이틀 위, 내용 아래)
            st.markdown(
                '<div style="display:flex; gap:2rem; margin-bottom:1rem; flex-wrap:wrap;">'
                f'<div style="flex:1; min-width:120px;"><div style="font-size:1.5rem; color:#6b7280;">기간</div><div style="font-size:1.75em;">{period_esc}</div></div>'
                f'<div style="flex:1; min-width:120px;"><div style="font-size:1.5rem; color:#6b7280;">Promotion name</div><div style="font-size:1.75em;">{pn_esc}</div></div>'
                f'<div style="flex:1; min-width:120px;"><div style="font-size:1.5rem; color:#6b7280;">Promotion info</div><div style="font-size:1.75em;">{pi_esc}</div></div>'
                '</div>',
                unsafe_allow_html=True,
            )

            if res.get("error"):
                st.error(res["error"])
                continue

            df: pd.DataFrame = res["df"]

            # 요금표 제목 + 수정/수정 완료 버튼(우측, 요금표와 가깝게)
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
                    if any(x in c for x in ['฿)', '(฿', '₩)', '(₩']) or c == '패키지세일(B, 맞춤 수식)' or c == "트리플 공급가(₩)":
                        display_for_style[c] = display_for_style[c].apply(
                            lambda x: f"{int(round(x)):,}" if isinstance(x, (int, float)) else x
                        )
                styled_preview = style_df(display_for_style)
                st.dataframe(styled_preview, use_container_width=True, height=fee_height, hide_index=True)
            else:
                # 수정 모드: data_editor + 수정 완료 버튼 (셀 포커스 상태에서 바로 버튼 눌러도 반영되도록 blur 스크립트는 data_editor 아래 주입)
                st.caption("맞춤 세일(฿)을 수정하면 트리플 공급가(₩)·마진이 자동으로 다시 계산됩니다. 셀 수정 후 바로 수정 완료를 눌러도 반영됩니다(잠시 후 처리).")
                column_config = {}
                for col in df.columns:
                    if col == "맞춤 세일(฿)":
                        column_config[col] = st.column_config.NumberColumn(col, format="%d")
                    else:
                        column_config[col] = st.column_config.Column(col, disabled=True)

                edited_df = st.data_editor(
                    df,
                    key=f"fee_editor_{i}_{idx}",
                    column_config=column_config,
                    use_container_width=True,
                    height=fee_editor_height,
                )
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
                    new_results = list(st.session_state.get("results") or [])
                    if 0 <= i < len(new_results):
                        new_results[i] = {**new_results[i], "df": recalc_df}
                    st.session_state["results"] = new_results
                    st.rerun()

            # CSV 다운로드 (현재 df 기준)
            current_df = st.session_state.get("results") or []
            csv_df = df
            for r in current_df:
                if r.get("idx") == idx and r.get("df") is not None:
                    csv_df = r["df"]
                    break
            st.markdown("---")
            csv = csv_df.to_csv(index=False, encoding='utf-8-sig')
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
                        _, export_sheet_name = _golf_mode_from_label(st.session_state.get("golf_mode_radio", GOLF_MODES[0][1]))
                        with st.spinner("구글 스프레드시트로 모든 요금표를 내보내는 중..."):
                            export_all_results_to_google_sheets(
                                st.session_state.get("results") or [],
                                sid,
                                sheet_name=export_sheet_name,
                                exchange_rate=st.session_state.get("exchange_rate", 0.0),
                            )
                        st.success("모든 요금표를 구글 스프레드시트로 내보냈습니다.")
                    except Exception as e:
                        st.error(f"내보내기 실패: {e}")
        else:
            st.caption("구글 시트 내보내기를 사용하려면 gspread 설치 및 서비스계정 시크릿 설정이 필요합니다.")


if __name__ == "__main__":
    main()
