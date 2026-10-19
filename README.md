# nvda-cds

[공개 대시보드](https://inandout-kr.github.io/nvda-cds/) · [원기록 및 수집 상태 JSON](https://inandout-kr.github.io/nvda-cds/data/cds.json)

DTCC SEC 공개 보고 자료에서 **직접 보고된 CDS 스프레드**를 확인하는 모니터입니다. 고정 쿠폰이나 upfront로 프리미엄을 역산하던 기존 경로는 공개 데이터 생성에서 제거했습니다. 구형 모델 시계열은 새 자료에 병합하지 않습니다.

## 현재 가능한 범위

2026-10-19 검증 시점에는 공개 원기록의 `Spread-Leg` 값과 `Spread notation-Leg` 코드는 확인했지만, **현재 DTCC SEC 명세에서 그 단위와 경제적 의미를 모두 인증하지 못했습니다.** DTCC의 최신 Message Specification 및 PPD Transformation/Scaling Help는 로그인 제한이 있습니다. 다른 SDR의 단위 코드를 DTCC에 대신 적용하지 않습니다.

따라서 운영 코드의 검증된 단위 매핑 `NOTATION_RULES`는 비어 있으며 **모든 BP 스프레드·일별 중앙값은 값 없음으로 표시**합니다. 원시 spread 필드는 원문 그대로 표시하고, 실제 프리미엄 또는 BP로 해석하지 않습니다. 원자료 확인과 결측 공개는 구현되었지만, 검증된 수치 프리미엄 제공은 해당 DTCC 정의를 확보할 때까지 제한됩니다. 쿠폰·upfront를 빈칸의 대체값으로 사용하지 않습니다.

## 표시·선별 정책

- 이름을 정규화한 뒤 허용된 **정확한 법인명**만 선택합니다. CoreWeave Inc와 loan CDS·금융 자회사·SPV를 합치지 않습니다. `DELL`의 대상 법인은 **Dell Inc**입니다.
- `UPI FISN = NA/CDS Corp SN Sr`, USD, 거래일 기준 **잔존만기 4–6년 구간**이 대상입니다. 표준화된 정확한 5Y 계약으로 부르지 않습니다. 원자료의 실제 만기를 함께 보여줍니다.
- 공개 필드에서 확인할 수 없는 documentation은 미확인으로 남깁니다. XR14를 가정하지 않습니다.
- 직접 spread, 단위, 경제적 의미, 통화, 거래 상태 등 모든 요건을 검증한 기록만 BP 집계에 포함할 수 있습니다. 부적격·모호한 필드를 임의 교정하거나 과거 수준에 맞춰 부호를 선택하지 않습니다.
- 동일 보고의 중복을 제거하고, 충돌하는 ID 및 정정·오류로 연결된 기록은 보수적으로 제외합니다. 원본·정정 ID, 거래·보고 시각, 실제 만기, 원시 spread/notation, 고정 쿠폰, upfront, 출처 파일을 보존합니다.
- 표시 숫자가 있을 경우 **최신 관측 거래일의 직접 보고 스프레드 일별 중앙값**이며 대상 보고 건수를 병기합니다. 개별 최신 체결가·딜러 종가·인증된 par spread가 아닙니다.
- 발행사마다 관측일이 다르므로 단순 기업 간 차이나 1D 변화율을 표시하지 않습니다. 차트는 관측점만 표시하며 결측 날짜를 보간하거나 이전 값으로 채우지 않습니다.
- 거래 시각, 보고 시각과 관측 보고 지연, 거래 후 경과 시간, 빌드 시각을 구분합니다. 파일 누락·오류·미공개와 실제 관측 부재는 별개입니다. 보고 기록 부재가 거래 부재를 뜻하지 않습니다.

## 구조

```text
scripts/dtcc.py          보고일별 DTCC 파일 다운로드·캐시·수집 상태
scripts/build_data.py    직접 보고 전용 선별·생애주기 검증·원기록 보존·일별 집계
scripts/validate.py      보존한 원기록에서 재계산해 공개 결과 검증
scripts/test_frontend.cjs 오프라인 프런트엔드 회귀 검사
docs/index.html         외부 UI 라이브러리가 필요 없는 정적 터미널 화면
docs/data/cds.json      schema_version=2, data_policy=direct-reported-only
docs/data/report-state.json  전체 원기록 재계산 상태 (공개 JSON이 해시·건수로 참조)
.github/workflows/      기존 수집·커밋 자동화
```

`scripts/cds.py`의 과거 모델 함수는 공개 대시보드 데이터의 입력으로 사용하지 않습니다. 기존 파일을 남겨도 프리미엄 추정 결과를 다시 노출하지 않습니다.

## 실행·검증

Python 표준 라이브러리를 사용합니다. 프런트엔드 회귀 검사는 Node.js가 필요합니다.

```bash
python scripts/build_data.py --full
python scripts/build_data.py
python scripts/build_data.py --full --offline
python scripts/validate.py
node scripts/test_frontend.cjs docs/data/cds.json
python -m unittest discover -s tests -v
python -m http.server 8765 --directory docs
```

`--full`은 보존 원기록과 이력을 새로 생성합니다. 기본 실행은 최근 보고 파일을 새로 확인하되 호환되는 직접 보고 전용 원기록만 이어받습니다. 구형/비호환 자료는 전체 재생성이 필요합니다. `--since YYYY-MM-DD`, `--end YYYY-MM-DD`로 보고 파일 범위를 지정할 수 있습니다. `--offline`은 캐시만 사용하며 확보하지 못한 파일을 공개합니다. 과거 실행일의 거래가 늦게 보고될 수 있으므로 보고일과 거래일은 같지 않을 수 있습니다.

보고 파일 날짜는 주말을 포함한 달력일 기준입니다. 오늘과 직전 2일 파일을 다시 확인하고, 이전 날짜는 캐시를 재사용하므로 오래된 파일에 대한 사후 수정이 자동 반영되지 않을 수 있습니다. 캐시 수정 시각과 실제 조회 시각, 파일 해시를 따로 기록합니다.

`validate.py`는 보존한 원기록에서 집계를 재계산해 이력·최신값·수집 범위가 일치하는지 검사합니다. 프런트엔드 검사는 전체 결측, 모든 감시 종목 표시, 구형 모델 페이로드 거부, HTTP/스키마 오류, 새 조회 실패 시 이전 숫자 제거, 결측 날짜 비보간, 다른 관측일 비교 미노출, 원시 필드와 HTML 이스케이프를 확인합니다. 브라우저 역시 정상 HTTP 응답 및 직접 보고 전용 스키마 검증에 실패하면 값 표시를 중단합니다.

## 수집 일정·배포

기존 GitHub Actions `update CDS data`는 평일 UTC **13:00–22:30의 30분 간격**, **23:45 추가 수집** 및 수동 실행을 사용합니다. `.github/workflows/update.yml`에서 실제 실행 경로를 확인할 수 있습니다. Actions 일정은 실행·게시 시각을 보장하지 않으며, 원천 지연이나 파일 미공개를 해결하지 않습니다. 브라우저의 5분 JSON 재조회도 실시간 거래 수집을 의미하지 않습니다.

기존 저장소의 `docs/` 공개 경로로 GitHub Pages를 배포합니다. 화면의 BUILD는 JSON 생성 시각이며 새 거래가 발생했다는 뜻이 아닙니다. 배포 후 공개 HTML과 JSON의 정책·스키마·원기록 및 최신 빌드 시각을 다시 확인해야 합니다.

## 공식 참고 자료·남은 제약

- [DTCC SEC Public Price Dissemination](https://pddata.dtcc.com/ppd/): 공개 파일 원천.
- [DTCC Public Price Dissemination Quick Reference Guide v2.8](https://kgc0418-tdw-data-0.s3.amazonaws.com/gtr/static/gtr/docs/RT_PPD_quick_ref_guide.pdf): 보고일별 파일 및 조회 구조. spread의 모든 경제적 의미를 인증하는 자료로 사용하지 않습니다.
- [DTCC GTR CFTC / Canada / SEC 자료실](https://dtcclearning.com/products-and-services/repository-services/gtr-cftc-canada-sec.html): 현재 Message Specification 및 PPD Transformation/Scaling Help의 접근 제한. 현 피드 정의를 확보하고 검증하기 전 단위 매핑을 추가하지 않습니다.
- [DTCC Info Center](https://pddata.dtcc.com/ppd/info-center): 자료 수정·중복 등 품질 안내.
- [SEC Release 34-63094](https://www.sec.gov/file/34-63094), p.13 각주 32: 거래 spread와 표준 100/500bp 계약 쿠폰의 구분.

공개 보고에는 지연·정정·오류·누락이 있을 수 있습니다. 이 도구는 유료 시세의 완전한 대체물이 아니며, 공개 원기록에서 검증할 수 있는 범위만 표시합니다. 최신 수치 제공을 위해 새 계정·유료 데이터·권한 확대를 자동으로 사용하지 않습니다.
