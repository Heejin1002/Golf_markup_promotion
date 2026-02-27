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
    골프 요금 HTML에서 데이터 추출.
    반환: list of dicts
    {
        hole, time_of_day, week_div,  # 18H / Morning / weekday
        site,                          # mk / ww
        net_thb, sale_thb,
        caddy_net, caddy_sale_thb,
        cart_net, cart_sale_thb,
    }
    """
    rows = []

    # ── 1. 각 hole 블록 추출
    hole_pattern = re.compile(
        r'btnholexgroupkey="hole-([^"]+)".*?(?=btnholexgroupkey="hole-|\Z)',
        re.DOTALL
    )

    # 전체 HTML을 hole별로 분리 ─ <tr btnholexgroupkey> 기준
    # btnholexgroupkey="hole-18H" 등
    hole_blocks = re.split(r'(?=<tr[^>]+btnholexgroupkey="hole-[^"]+")', html)

    current_hole = None
    for block in hole_blocks:
        # 현재 hole 이름 확인
        hole_match = re.search(r'btnholexgroupkey="hole-([^"]+)"', block)
        if hole_match:
            candidate = hole_match.group(1)
            # <th> 안에 있는 진짜 hole 선언인지 확인 (hidden input으로)
            hole_hidden = re.search(
                rf'name="golf_rate\.rateJson\.renderRow\.{re.escape(candidate)}"',
                block
            )
            if hole_hidden:
                current_hole = candidate

        if current_hole is None:
            continue

        # ── 캐디피 / 카트피 (hole 단위 공통)
        caddy_net = _extract_val(block, rf'name="golf_rate\.rateJson\.caddy\.{re.escape(current_hole)}\.nett"')
        caddy_sale_thb = _extract_val(block, rf'name="golf_rate\.rateJson\.caddy\.{re.escape(current_hole)}\.sale\.THB"')
        cart_net = _extract_val(block, rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(current_hole)}\.nett"')
        cart_sale_thb = _extract_val(block, rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(current_hole)}\.sale\.THB"')

        # ── iveTrNett 행들 (각 시간대) 파싱
        time_blocks = re.split(r'(?=<tr[^>]+class="[^"]*iveTrNett[^"]*")', block)
        for tb in time_blocks:
            if 'iveTrNett' not in tb:
                continue

            # 시간대 추출 (column-fixed-1 의 th 텍스트)
            time_match = re.search(r'class="column-fixed-1">([^<]+)</th>', tb)
            if not time_match:
                continue
            time_of_day = time_match.group(1).strip()

            # mk(monkey) 세일가만 사용
            for week_div in ('weekday', 'weekend'):
                net_key = rf'name="golf_rate\.rateJson\.{week_div}\.{re.escape(current_hole)}\.{re.escape(time_of_day)}\.nett"'
                sale_key = rf'name="golf_rate\.rateJson\.{week_div}\.{re.escape(current_hole)}\.{re.escape(time_of_day)}\.sale\.monkey\.THB"'

                net_val = _extract_val(tb, net_key)
                sale_val = _extract_val(tb, sale_key)

                if (sale_val is None or sale_val == 0) and (net_val is None or net_val == 0):
                    continue

                # 캐디/카트 status (block 전체에서 시간대별로 추출)
                caddy_status = _extract_status(
                    block,
                    rf'name="golf_rate\.rateJson\.caddy\.{re.escape(current_hole)}\.{re.escape(time_of_day)}\.caddyStatus"'
                )
                cart_status = _extract_status(
                    block,
                    rf'name="golf_rate\.rateJson\.cart1pax\.{re.escape(current_hole)}\.{re.escape(time_of_day)}\.cartStatus"'
                )

                rows.append({
                    'hole': current_hole,
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


def build_table(rows, exchange_rate, commission_rates, discount_rate):
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
            '캐디피(세일, ฿)': caddy_sale,
            '카트피(넷, ฿)': cart_net,
            '카트피(세일, ฿)': cart_sale,
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

            # 최종판매가 = 세일가_원화 × (1 - 할인율)
            final_price_krw = round(pkg_sale_krw * (1 - discount)) if exchange_rate > 0 else 0
            commission_krw = round(final_price_krw * comm_d)
            supply_krw = final_price_krw - commission_krw
            margin_krw = supply_krw - pkg_net_krw

            # 필요 마크업 (넷가 > 공급가일 때)
            supply_thb = pkg_sale * (1 - comm_d)
            req_markup = 0
            if supply_thb > 0 and supply_thb < pkg_net:
                req_markup = math.ceil((pkg_net / supply_thb - 1) * 100)

            rec[f'필요마크업_{comm_str}%'] = f"{req_markup}%" if req_markup > 0 else "0%"
            if exchange_rate > 0:
                rec[f'최종판매가_{comm_str}%(₩)'] = final_price_krw
                rec[f'공급가_{comm_str}%(₩)'] = supply_krw
                rec[f'마진_{comm_str}%(₩)'] = margin_krw

        records.append(rec)

    return pd.DataFrame(records)


# ─────────────────────────────────────────────
#  스타일
# ─────────────────────────────────────────────
def style_df(df):
    def highlight(row):
        styles = [''] * len(row)
        # 마진 마이너스 → 행 전체 빨강
        for i, col in enumerate(row.index):
            if '마진' in col:
                try:
                    v = float(str(row[col]).replace(',', '').replace('원', ''))
                    if v < 0:
                        return ['background-color: #fee2e2; color: #dc2626; font-weight: bold'] * len(row)
                except:
                    pass
        # 필요마크업 > 0 → 셀 빨강
        for i, col in enumerate(row.index):
            if '필요마크업' in col:
                try:
                    v = float(str(row[col]).replace('%', ''))
                    if v > 0:
                        styles[i] = 'background-color: #fee2e2; color: #dc2626; font-weight: bold'
                except:
                    pass
        # 주말/연휴 행 → 연한 노랑
        if '주중/주말' in row.index and row['주중/주말'] == '주말/연휴':
            return ['background-color: #fefce8'] * len(row)
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
        ('discount_rate', 0.0),
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
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        exchange_input = st.text_input("환율 (THB → KRW)", placeholder="예: 43.5", value="")
    with col2:
        commission_input = st.text_input("수수료 (%)", placeholder="예: 4,6.6,10", value="")
    with col3:
        discount_input = st.text_input("할인율 (%)", placeholder="예: 0", value="")
    with col4:
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

            try:
                discount_rate = float(discount_input.strip()) if discount_input.strip() else 0.0
            except:
                discount_rate = 0.0

            rows = parse_golf_html(html_input)

            if not rows:
                st.error("HTML에서 골프 요금 데이터를 찾지 못했습니다. 올바른 골프 요금표 HTML인지 확인해 주세요.")
            else:
                df = build_table(rows, exchange_rate, commission_rates, discount_rate)
                st.session_state['result_df'] = df
                st.session_state['exchange_rate'] = exchange_rate
                st.session_state['commission_rates'] = commission_rates
                st.session_state['discount_rate'] = discount_rate
                st.success(f"✅ {len(rows)}개 요금 항목 파싱 완료!")
                st.rerun()

    # ── 결과 표시
    if st.session_state['result_df'] is not None:
        df: pd.DataFrame = st.session_state['result_df']
        exchange_rate = st.session_state['exchange_rate']
        commission_rates = st.session_state['commission_rates']
        discount_rate = st.session_state['discount_rate']

        st.markdown("---")

        # 설정 요약
        c1, c2, c3 = st.columns(3)
        c1.metric("환율", f"1 THB = {exchange_rate:,.2f} KRW" if exchange_rate > 0 else "미설정")
        c2.metric("수수료", ", ".join([f"{x}%" for x in commission_rates]) if commission_rates else "미설정")
        c3.metric("할인율", f"{discount_rate}%")

        st.markdown(f"### 결과 테이블 ({len(df)}개 항목)")

        # 숫자 컬럼 포맷팅 (표시용 복사본)
        display_df = df.copy()
        for col in display_df.columns:
            if any(k in col for k in ['฿)', '(฿', '₩)', '(₩']):
                display_df[col] = display_df[col].apply(
                    lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x
                )

        styled = style_df(display_df)
        st.dataframe(styled, use_container_width=True, height=600)

        # ── 주중/주말 비교 요약 섹션
        st.markdown("---")
        st.markdown("### 📊 주중 vs 주말 패키지 가격 비교")

        for hole in df['홀'].unique():
            hole_df = df[df['홀'] == hole]
            
            # '사이트' 컬럼이 있는지 확인
            if '사이트' in hole_df.columns:
                sites = hole_df['사이트'].unique()
            else:
                # '사이트' 컬럼이 없으면 전체 hole_df를 하나의 그룹으로 처리
                sites = [None]
            
            for site in sites:
                if site is not None:
                    site_df = hole_df[hole_df['사이트'] == site]
                    site_label = f" | 사이트: {site}"
                else:
                    site_df = hole_df
                    site_label = ""

                wd_rows = site_df[site_df['주중/주말'] == '주중']
                we_rows = site_df[site_df['주중/주말'] == '주말/연휴']

                if wd_rows.empty and we_rows.empty:
                    continue

                st.markdown(f"#### 🏌️ {hole}{site_label}")

                compare_records = []
                for time_of_day in site_df['시간대'].unique():
                    td_df = site_df[site_df['시간대'] == time_of_day]
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