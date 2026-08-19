# nvda-cds

엔비디아(및 AI 크레딧 피어)의 **5년 CDS 스프레드**를 무료 공개 데이터로 추적하는 정적 사이트.

👉 https://inandout-kr.github.io/nvda-cds/

## 왜 만들었나

Bloomberg·Markit·ICE의 CDS 호가는 전부 유료다. 대신 미국 SEC 규정에 따라
**DTCC가 무료·무인증으로 공개하는 스왑 체결 보고(Public Price Dissemination)** 를 쓴다.
호가가 아니라 **실제 체결된 거래**라서, 뉴스에 인용되는 종가 컴포짓과는 몇 bp 차이가 난다.

## 구조

```
scripts/dtcc.py        DTCC 일별 파일 다운로드/캐시
scripts/cds.py         ISDA 방식 업프론트 ↔ 파 스프레드 환산
scripts/build_data.py  파싱 → 일별 스프레드 집계 → docs/data/cds.json
scripts/validate.py    홀드아웃 정확도 검증
docs/index.html        정적 페이지 (빌드 없음, 바닐라 JS)
.github/workflows/     30분 주기 갱신 + 커밋
```

## 산출 방법

단일종목 CDS는 표준 쿠폰(IG 100bp, HY 500bp) + 업프론트 현금으로 거래되므로,
보고서에 스프레드가 아닌 현금액으로 실리는 경우가 많다. 이를 역산한다.

```
현금 = (쿠폰 − 스프레드) × RPV01(스프레드) + 경과이자
```

회수율 40%, 균일 부도강도 `h = S/(1−R)`, 분기 ACT/360 IMM 쿠폰, 균일 할인율 4%.
(할인율 100bp 변화당 스프레드 영향은 약 0.4bp로, 체결 노이즈보다 작다.)

DTCC는 업프론트를 **부호 없이** 공개하기 때문에 쿠폰 위·아래 두 해가 나온다.
같은 날 스프레드로 직접 보고된 체결이 있으면 그걸 기준선으로, 없으면 직전 레벨을 기준으로 분기를 고른다.

## 검증

업프론트와 스프레드가 **둘 다** 실린 종목일을 홀드아웃으로 두고,
업프론트만으로 복원한 값을 실제 보고 스프레드와 비교한다.

```bash
python scripts/validate.py
```

| | 오차 중앙값 | 90분위 | 최대 |
|---|---|---|---|
| 전체 (360 종목일) | 0.82bp | 6.8bp | 28bp |
| NVIDIA (23일) | 0.86bp | 1.9bp | 2.1bp |

외부 교차검증 — 2026-07-29 보도치 83.7bp ↔ 본 산출 81.8bp,
2026-07-27 보도치 82bp ↔ 78.1bp.

## 로컬 실행

```bash
python scripts/build_data.py --full      # 전체 이력 재생성 (DTCC 파일 200여 개 다운로드)
python scripts/build_data.py             # 최근 10일만 갱신
python -m http.server 8765 --directory docs
```

의존성 없음 (표준 라이브러리만).

## 한계

- **실시간이 아니다.** GitHub Actions가 미국장 중 30분 주기로 갱신한다.
- NVIDIA 5년 CDS는 2025-11-20부터 체결이 잡힌다. 그 이전 값은 없다.
- 체결이 없는 날은 값이 없다.
- 공개 보고의 명목금액은 `5,000,000+`로 상한 처리되어 대형 블록은 업프론트 비율이 왜곡될 수 있다.
  일별 중앙값 + 기준선 ±50% 밖 체결 제외로 걸러낸다.

투자 자문이 아니다.
