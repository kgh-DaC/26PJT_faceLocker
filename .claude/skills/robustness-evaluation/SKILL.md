---
name: robustness-evaluation
description: 얼굴인식의 신뢰도와 인식률을 평가한다. FAR/FRR/TAR, ROC, DET, EER, 혼동행렬, 점수 분포, 조건별(조명·마스크·안경·목도리·각도) 성능, 임계값 보정, 부트스트랩 신뢰구간, HTML 리포트 생성. 학습 데이터 신뢰도 점검, 평가 항목 표·그래프 요청, 임계값 조정, 성능 비교·재평가 시 사용한다.
---

# 강건성·신뢰도 평가

## 데이터 규약
```
data/gallery/<person>/<tag>_<n>.(jpg|npy)     # 등록용
data/probe/<condition>/<person>/*.jpg          # 시험용 (gallery와 겹치면 안 됨)
data/probe/_impostor/<id>/*.jpg                # 등록되지 않은 사람 (FAR 계산에 필수)
```
조건 폴더 이름 예: `normal, lowlight, backlight, mask, glasses, noglasses, scarf, angle`. impostor가 없으면 FAR을 계산할 수 없으므로, 등록자끼리의 교차 비교(다른 사람 쌍)를 imposter로 쓰고 리포트에 그 사실을 적는다. 실제 미등록자 데이터보다 낙관적이다.

`scripts/make_synthetic_probes.py`로 정상 이미지에서 합성 조건(감마·밝기·노이즈·블러·가상 마스크/안경 오버레이)을 만들 수 있다. 이 결과는 `condition`에 `syn_` 접두어를 붙여 실촬영과 섞이지 않게 한다.

## 지표와 의미 (리포트에 한 줄 설명 포함)
| 지표 | 의미 | 보안 관점 |
|---|---|---|
| FAR | 타인을 등록자로 오인해 문이 열린 비율 | 가장 중요. 목표 ≤ 0.1% |
| FRR | 등록자를 거부한 비율 | 편의성 |
| TAR = 1-FRR | 올바른 개방률 | |
| EER | FAR=FRR 지점 | 모델 비교용, 운영점 아님 |
| ROC / DET | 임계값별 TAR-FAR, FRR-FAR 곡선 | 임계값 선택 근거 |
| 혼동행렬 | 등록자별 식별 결과(+Unknown) | 혼동되는 쌍 확인 |
| 점수 분포 | genuine vs impostor 히스토그램 | 분리도(d′) |
| 조건별 표 | 위 지표를 조건별로 | 약한 조건 발견 |
| 지연시간/FPS | 프레임당 처리 시간 | Pi5 실시간성 |
| 보류율(HOLD) | 품질 미달로 판정 보류된 비율 | 사용성 |

## 절차
1. **분리 확인**: gallery와 probe 파일 해시 중복을 검사해 누수를 막는다.
2. **보정 vs 평가 분할**: probe를 보정(calibration)용과 최종 평가용으로 분할(사람 단위 또는 이미지 단위 층화). 보정셋에서 목표 FAR에 해당하는 임계값을 구해 `models/calibration.json`(`global`, 필요 시 `mask`)에 저장한다. 평가셋은 이 임계값으로 한 번만 채점한다.
3. **지표 계산**: `metrics.py`는 점수 배열(genuine/impostor)로부터 순수 함수로 계산하고, 작은 손계산 예제 단위 테스트를 둔다. FAR 0이 나오면 "표본 N에서 0건"이라고 쓰고 95% 상한(Rule of three: 3/N)을 같이 적는다.
4. **신뢰구간**: 사람/이미지 단위 부트스트랩 1000회로 95% CI.
5. **리포트**: `reports/<timestamp>/`에 CSV, PNG, `report.html`(요약 → 조건별 표 → 그래프 → 한계·주의). 그래프 색·라벨은 `dataviz` 스킬 원칙을 따른다. 맨 위에 표본 수와 합성 데이터 비중을 쓴다.
6. **운영 로그 평가**: `logs/events.csv`에 사용자가 정/오 라벨(`--label`)을 붙여 실사용 정확도를 같은 지표로 재계산한다. 이것이 "추후 얼마나 올바르게 평가되었는지" 판단하는 경로다.
7. **회귀 비교**: 이전 `metrics.json`과 비교해 조건별 FRR/FAR 변화표를 리포트에 넣는다. 모델·임계값·증강을 바꿀 때마다 재실행한다.

## 해석 가이드
- 마스크/목도리 조건의 FRR이 높으면: 해당 조건 갤러리 샘플 추가 → 그래도 부족하면 마스크 전용 임계값(FAR 재측정 필수) → 최후에 "마스크 시 2차 인증 요구" 정책을 제안한다. 임계값만 낮춰 FRR을 맞추는 것은 FAR을 악화시키므로 금지한다.
- 조명: 저조도에서 HOLD 비율이 높으면 정상이다. 오탐으로 열리는 것보다 낫다. 조명/IR 보조광 하드웨어를 제안한다.
- d′ 또는 분포 겹침이 크면 데이터 문제(등록 품질)인지 모델 한계인지 구분해 보고한다.

## 실행
`python scripts/evaluate.py --gallery data/gallery --probe data/probe --target-far 0.001 --out reports/`
