import streamlit as st
import re
import pandas as pd
import math

st.set_page_config(page_title="골프 요금 마크업 계산기", layout="wide")


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

    # 주중 베이직 요금 계산을 위해 weekday 데이터 먼저 수집
    # key: (hole, time_of_day, site) → basic_total (그린피세일+카트세일+캐디세일)
    weekday_basics = {}
    for r in rows:
        if r['week_div'] == 'weekday':
            basic = r['sale_thb'] + r['caddy_sale_thb'] + r['cart_sale_thb']
            weekday_basics[(r['hole'], r['time_of_day'])] = basic

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

        # 주말/연휴 판매가 증가율 (베이직 = 주중 세일 패키지 합계)
        basic_total = weekday_basics.get((hole, time_of_day), 0)
        if basic_total > 0 and week_div != 'weekday':
            increase_rate = round((pkg_sale / basic_total - 1) * 100, 2)
            increase_str = f"{increase_rate:+.2f}%"
        elif week_div == 'weekday':
            increase_str = "기준"
        else:
            increase_str = "-"

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
            '판매가증가율': increase_str,
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
            # 조정이 필요한 경우: 목표마진율 입력(>0) OR 마진이 음수
            need_adjust = (min_margin_rate > 0) or (margin_krw < 0)
            if need_adjust and exchange_rate > 0 and (1 - comm_d) > 0:
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
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── 입력 영역
    col_input, col_clear = st.columns([5, 1])
    with col_input:
        html_input = st.text_area(
            "골프 요금표 HTML 붙여넣기",
            placeholder="<div class=\"table-responsive table-fixed-rate\">...",
            height=250,
            key=f"html_input_{st.session_state['html_key']}"
        )
    with col_clear:
        st.write(""); st.write("")
        if st.button("🗑️ Clear", use_container_width=True):
            st.session_state['html_key'] += 1
            st.session_state['result_df'] = None
            st.rerun()

    # ── 파라미터 입력
    col1, col2, col3 = st.columns(3)
    with col1:
        exchange_input = st.text_input("환율 (THB → KRW)", placeholder="예: 43.5", value="")
    with col2:
        commission_input = st.text_input("수수료 (%)", placeholder="예: 4,6.6,10", value="")
    with col3:
        st.write("")
        st.write("")
        calc_btn = st.button("🔢 계산하기", type="primary", use_container_width=True)

    # ── 계산 실행
    if calc_btn:
        if not html_input.strip():
            st.error("HTML을 입력해 주세요.")
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

            rows = parse_golf_html(html_input)

            if not rows:
                st.error("HTML에서 골프 요금 데이터를 찾지 못했습니다. 올바른 골프 요금표 HTML인지 확인해 주세요.")
            else:
                df = build_table(rows, exchange_rate, commission_rates, 0.0, min_margin_rate)
                st.session_state['result_df'] = df
                st.session_state['exchange_rate'] = exchange_rate
                st.session_state['commission_rates'] = commission_rates
                st.session_state['min_margin_rate'] = min_margin_rate
                st.session_state['_html_cache'] = html_input
                st.success(f"✅ {len(rows)}개 요금 항목 파싱 완료!")
                st.rerun()

    # ── 결과 표시
    if st.session_state['result_df'] is not None:
        df: pd.DataFrame = st.session_state['result_df']
        exchange_rate = st.session_state['exchange_rate']
        commission_rates = st.session_state['commission_rates']
        min_margin_rate = st.session_state.get('min_margin_rate', 0.0)

        st.markdown("---")

        # 설정 요약
        c1, c2 = st.columns(2)
        c1.metric("환율", f"1 THB = {exchange_rate:,.2f} KRW" if exchange_rate > 0 else "미설정")
        c2.metric("수수료", ", ".join([f"{x}%" for x in commission_rates]) if commission_rates else "미설정")

        # ── 목표 마진율 입력 (결과 테이블 상단)
        st.markdown(f"### 결과 테이블 ({len(df)}개 항목)")
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
                parsed_rows = parse_golf_html(st.session_state.get('_html_cache', ''))
                if parsed_rows:
                    df = build_table(
                        parsed_rows,
                        st.session_state['exchange_rate'],
                        st.session_state['commission_rates'],
                        0.0,
                        new_margin
                    )
                    st.session_state['result_df'] = df
                st.rerun()

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
            # th 셀렉터: Streamlit dataframe 헤더는 th[data-testid="column-header-cell"] 내부 span
            css_rules = " ".join([
                f'div[data-testid="stDataFrame"] thead tr th:nth-child({i+2}) div {{ font-weight: 900 !important; }}'
                for i in bold_col_indices
            ])
            st.markdown(f"<style>{css_rules}</style>", unsafe_allow_html=True)

        st.dataframe(styled, use_container_width=True, height=600)

        # ── 주중/주말 비교 요약 섹션
        st.markdown("---")
        st.markdown("### 📊 주중 vs 주말 패키지 가격 비교")

        for hole in df['홀'].unique():
            hole_df = df[df['홀'] == hole]

            wd_rows = hole_df[hole_df['주중/주말'] == '주중']
            we_rows = hole_df[hole_df['주중/주말'] == '주말/연휴']

            if wd_rows.empty and we_rows.empty:
                continue

            st.markdown(f"#### 🏌️ {hole}")

            compare_records = []
            for time_of_day in hole_df['시간대'].unique():
                td_df = hole_df[hole_df['시간대'] == time_of_day]
                wd = td_df[td_df['주중/주말'] == '주중']
                we = td_df[td_df['주중/주말'] == '주말/연휴']

                def get_val(sub_df, col, default=0):
                    if not sub_df.empty and col in sub_df.columns:
                        v = sub_df.iloc[0][col]
                        if isinstance(v, (int, float)):
                            return v
                        try:
                            return int(str(v).replace(',', ''))
                        except:
                            return default
                    return default

                wd_net = get_val(wd, '그린피(넷, ฿)')
                wd_sale = get_val(wd, '그린피(세일, ฿)')
                wd_pkg_net = get_val(wd, '패키지넷(฿)')
                wd_pkg_sale = get_val(wd, '패키지세일(฿)')
                we_net = get_val(we, '그린피(넷, ฿)')
                we_sale = get_val(we, '그린피(세일, ฿)')
                we_pkg_net = get_val(we, '패키지넷(฿)')
                we_pkg_sale = get_val(we, '패키지세일(฿)')

                wd_rate_str = wd.iloc[0]['판매가증가율'] if not wd.empty else '-'
                we_rate_str = we.iloc[0]['판매가증가율'] if not we.empty else '-'

                compare_records.append({
                    '시간대': time_of_day,
                    '구분': '주중',
                    '그린피 넷(฿)': f"{wd_net:,}" if wd_net else '-',
                    '그린피 세일(฿)': f"{wd_sale:,}" if wd_sale else '-',
                    '패키지 넷(฿)': f"{wd_pkg_net:,}" if wd_pkg_net else '-',
                    '패키지 세일(฿)': f"{wd_pkg_sale:,}" if wd_pkg_sale else '-',
                    '증가율': wd_rate_str,
                })
                compare_records.append({
                    '시간대': time_of_day,
                    '구분': '주말/연휴',
                    '그린피 넷(฿)': f"{we_net:,}" if we_net else '-',
                    '그린피 세일(฿)': f"{we_sale:,}" if we_sale else '-',
                    '패키지 넷(฿)': f"{we_pkg_net:,}" if we_pkg_net else '-',
                    '패키지 세일(฿)': f"{we_pkg_sale:,}" if we_pkg_sale else '-',
                    '증가율': we_rate_str,
                })

            if compare_records:
                compare_df = pd.DataFrame(compare_records)

                def style_compare(row):
                    if row['구분'] == '주말/연휴':
                        return ['background-color: #fef9c3'] * len(row)
                    return [''] * len(row)

                st.dataframe(
                    compare_df.style.apply(style_compare, axis=1),
                    use_container_width=True,
                    hide_index=True
                )

        # CSV 다운로드
        st.markdown("---")
        csv = df.to_csv(index=False, encoding='utf-8-sig')
        st.download_button(
            label="📥 CSV 다운로드",
            data=csv,
            file_name="golf_markup_result.csv",
            mime="text/csv"
        )


if __name__ == "__main__":
    main()
