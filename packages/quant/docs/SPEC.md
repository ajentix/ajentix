# Deep Interview Spec: ajentix-quant — 결정론적 마켓뉴트럴 퀀트 머니메이킹 시스템

## Metadata
- Interview ID: 9D60062A-C1F9-40FD-A5D7-0B966E304702
- Rounds: 25 (Round 0 토폴로지 + R1~R25)
- Final Ambiguity Score: 5% (closure 가드·restate 게이트 통과)
- Type: greenfield
- Generated: 2026-06-18
- Threshold: 0.05
- Threshold Source: default
- Status: PASSED (acceptance-guard + restate-gate cleared)
- Auto-Researched Rounds: [] (모든 결정은 사용자 판단)
- Auto-Answered Rounds: []
- Architect Failures: 0
- Lateral Reviews: 2 (R5 initial→progress, R13 progress→refined; researcher/contrarian/simplifier/architect 인라인)
- Lateral Panel Failures: 0
- Refined Rounds: R1,R2,R3,R5,R6,R8,R9,R11,R12,R18,R19,R20,R21,R22,R23,R24,R25
- Closure Overrides: 2 (R18 "너무 미니멀"→확장; R20 "너무 큼"→walking-skeleton 중간; R22~R25 레버리지/리스크 심화)
- Restated Goal: "목표 = risk-adjusted 수익 극대화. 수단 = Bybit(ccxt)에서 시작하는 베뉴-어댑터 기반 결정론적 마켓뉴트럴 펀딩-하베스트 퀀트 봇(런타임 LLM=0, 빌드·유지는 AI 에이전트). 검증 = $500~2000 소액으로 백테스트→페이퍼→소액라이브 3단계 게이트로 엣지를 증명하고, HL·CeDeFi·스탯아브로 확장 가능한 엔진을 남긴다."
- Web Research Passes: 13+ (DeFi 수익전략·MEV·펀딩/베이시스·솔버·quant Sharpe·LVR/OEV·레버리지/청산/베이시스·포트폴리오마진/ADL·옵션 테일헷지)

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.96 | 0.40 | 0.384 |
| Constraint Clarity | 0.94 | 0.30 | 0.282 |
| Success Criteria | 0.90 | 0.30 | 0.270 |
| **Total Clarity** | | | **0.936** |
| **Ambiguity** | | | **0.064** |

## Topology
| Component | Status | Description | Coverage / Deferral Note |
|-----------|--------|-------------|--------------------------|
| autonomy-orchestration | active | 결정론적 전략 실행을 24/7 돌리는 자율 프로세스(런타임 LLM=0). LLM 에이전트는 빌드·유지·리서치·저빈도 감독에만 | 런타임 LLM=0 HARD 제약 + 에이전트=개발수단 |
| opportunity-detection | active | 베뉴-aware microstructure 시그널로 펀딩/베이시스 기회 탐지·시뮬 | 코어=델타뉴트럴 펀딩 하베스트, Bybit v1 |
| execution-engine | active | ccxt 어댑터 추상화 위 주문·헤지·리밸런싱(결정론) | Bybit 어댑터 v1 + 어댑터 패턴 |
| risk-capital-keys | active | 동적 레버리지·청산버퍼·ADL·킬스위치·거래전용키 | R22~R25 완전 리스크 모델로 커버 |
| infra-scaffolding | active | 모노레포·데이터·백테스트 하네스·CI·secrets | walking-skeleton(Phase 0)로 커버 |

## Established Facts
1. greenfield, 결정론적 마켓뉴트럴 퀀트 "돈 버는 시스템".
2. 근본 동기 = 돈이 본질(risk-adjusted 수익 극대화). DeFi/온체인은 수단·옵션, 하드 제약 아님. 더 벌면 CEX·CeDeFi도 수용.
3. 전략군 = 델타뉴트럴 펀딩/베이시스 하베스트(코어), 스탯아브 후순위 위성. 알파 원천 = 베뉴-aware microstructure(균일추상화로 안 뭉갬, 플러밍만 추상화).
4. 바닐라 MEV/DEX 차익은 신규진입 레드오션 → 코어 제외.
5. 런타임 LLM=0(결정론). LLM 에이전트는 빌드·유지·리서치·저빈도 감독 전용.
6. 자본 = $500~2000 레인지 시작, 이후 스케일업. v1 목표 = 엣지 증명 + 확장 엔진(절대수익 아님).
7. v1 = 단일 CEX(ccxt) 델타뉴트럴 펀딩 하베스트. 어댑터 추상화 → HL·CeDeFi 후속.
8. v1 레퍼런스 CEX = Bybit(펀딩 스큐 강함, 통합마진, ccxt, 무료 과거데이터).
9. repo = ajentix/ajentix-quant, private, yeongjunyoo push.
10. 스펙=풀 비전, 세션 실행=walking skeleton(중간, 핵심경로 동작).
11. 레버리지 = 1급 동적 리스크 파라미터(R22~R25): 정적 1x 아님. 포트폴리오마진(헤지네팅)으로 자본효율 + ADL 후순위.

## Trigger Metadata
- R1~R2 (D): MEV 직감 → 레드오션 판명, 마켓뉴트럴 재정렬.
- R11 (D): "왜 HL만?" → DeFi-vs-CeFi 근본 스코프 재개방(32→38%).
- R12 (해소): money-first 확정(38→32%). "순수 온체인" lean은 상위결정으로 정상 대체.
- R18/R20 (closure override): 미니멀↔과대 → 스펙=크게/세션=walking-skeleton 분리.
- R22~R25 (constraint 심화): 레버리지 단순 1x → 동적 레짐-aware + 포트폴리오마진 + 테일헷지 로드맵.

## Lateral Review Panel
- R5 (initial→progress): contrarian=자본규모 리스크; simplifier=단일전략·단일베뉴; architect=모듈 분리.
- R13 (progress→refined): contrarian=음의펀딩/거래소리스크 생존; simplifier=백테스트+페이퍼면 충분; architect=API키 거래전용·출금금지+리스크엔진.

## Goal
risk-adjusted 수익 극대화를 목표로, Bybit(ccxt)에서 시작하는 베뉴-어댑터 기반 결정론적 마켓뉴트럴 델타뉴트럴 펀딩-하베스트 퀀트 봇을 구축한다. 현물 롱 + 무기한 숏으로 시장중립을 유지하며 양(+)의 펀딩을 수확하고, 동적 레짐-aware 레버리지와 리스크 한도를 결정론 코드로 관리한다. 런타임 LLM=0(매매=순수 결정론), AI 에이전트는 빌드·유지·리서치·저빈도 감독 전용. $500~2000 소액으로 3단계 게이트(백테스트→페이퍼→소액 라이브)로 엣지를 증명하고, 이후 Hyperliquid 롱테일 펀딩·CeDeFi 크로스벤뉴·스탯아브로 확장 가능한 엔진을 남긴다.

## Constraints
- 런타임 LLM=0: 매매 핫패스 100% 결정론 코드.
- money-first: 베뉴는 도구, risk-adjusted 수익 최우선.
- 자본 $500~2000 시작: 가스리스/저가/저KYC마찰 선호. v1=Bybit.
- 마켓뉴트럴: 방향성 베팅 금지.
- 베뉴 추상화: 플러밍만 통일, microstructure 시그널 1급 노출.
- 키 정책: API 키 거래전용·출금권한 절대 금지. secrets는 .env(커밋 금지).
- 스택: Python 3.12 + ccxt + pandas/polars + pydantic + pytest + ruff + mypy.
- v1 페어: BTC·ETH 등 메이저 1~2개부터.

## Risk & Leverage Model (R22~R25 — 핵심)
- **레버리지 = 동적 레짐-aware 1급 파라미터**(정적 1x 아님): 베이스 2~3x, 저변동성+고(+)펀딩 레짐에서만 캡 5x까지 lever-up, 변동성 스파이크·펀딩 압축/반전 임박·청산거리 임박 시 오토디레버리지. 알트는 더 낮게, >5x 금지.
- **포트폴리오/크로스 마진(헤지 네팅)**: 롱현물+숏perp 델타뉴트럴을 거래소가 인식 → 자본효율 ↑ + ADL 큐 후순위(OKX 명시, HL portfolio margin). 단 크로스 델타뉴트럴 계정은 캐스케이드에 과대표집됨 → ADL-rank 모니터링 필수.
- **청산거리 ≥15% 이격** 강제, health factor ≥~1.5, 버퍼 미달 시 오토디레버리지.
- **비상준비금 20~30%** 미할당 스테이블(스트레스 시 마진 top-up).
- **펀딩반전 강제 exit**: 음의 펀딩 지속 임계 초과 시 청산(72h+ 버티기 금지). 음의펀딩은 빨리 회복(최장 ~13일).
- **갭/캐스케이드 생존**: 레버리지 캡을 "문서화된 최악 갭(≥15~20%, 예: 2026-10 플래시크래시)에도 청산 안 됨"으로 설정. 백테스트에 해당 스트레스 강제.
- **포지션 사이징**: 단일 셋업 ≤25% 자본 → $500~2000이면 1~2 동시 포지션.
- **옵션 테일헷지(Phase 3+ 레버리지 언락)**: 숏 perp 레그의 위쪽 갭을 OTM 콜/콜스프레드로 캡, collar/리스크리버설로 비용 충당. 베뉴=Deribit(OI 85%, 블록트레이드 헤지레그 수수료0)·Bybit(USDC 유럽형, 동일베뉴)·Aevo/Derive(온체인). 비용 ~연 1.5~2% 드래그. 단독 알파 아님(GS ~0.8bps)이나 "무한 갭/ADL 테일→알려진 비용" 전환으로 더 높은 레버리지를 안전하게 → 복리의 분산세 절감(Universa/One River). $500~2000 v1엔 운영부담 과해 보류.

## Non-Goals (이번 세션/현재 제외)
- 라이브 실주문(이번 세션). 페이퍼/드라이런만.
- LLM 기반 매매 의사결정(영구 제외).
- 바닐라 ETH MEV/레이턴시 차익(레드오션).
- 스탯아브·HL·CeDeFi 어댑터·옵션 테일헷지 풀구현(후속 Phase).
- 풀 백테스트 방법론·모니터링 대시보드·포트폴리오 배분(후속 Phase).

## Acceptance Criteria
### 제품(전체) 성공 게이트 — 3단계 (수치=디폴트, 튜닝 가능)
- [ ] 백테스트: 수수료+양방향 펀딩+사이즈별 슬리피지+동적 레버리지 비용 차감 net, 음의펀딩 구간 ≥1회 + 플래시크래시류 갭 스트레스 포함 기간에서 연율 Sharpe ≥ 1.5, MDD ≤ 5%(델타뉴트럴 기준), net APR ≥ 0.
- [ ] 페이퍼/극소액 라이브(2~4주): |순델타| 밴드 내, net PnL ≥ 0, MDD ≤ 5%, 킬스위치·청산 미발동, ADL 미발동.
- [ ] 소액 라이브 확대: 위 통과 시 단계적 증액.
- [ ] 수익률 타깃(근거): v1 베이스 net 10~15%, 스트레치 20~30%, 로드맵 후기(롱테일/CeDeFi/레버리지언락) 30~150% 잠재.

### 이번 세션(Phase 0, walking skeleton) 성공 기준
- [ ] .gjc/specs/deep-interview-ajentix-quant.md 풀 비전 스펙 영속화.
- [ ] gh 활성계정 yeongjunyoo로 switch.
- [ ] ajentix/ajentix-quant private repo 생성·push.
- [ ] 모노레포 골격 + pyproject + CI + README + .gitignore.
- [ ] adapters/bybit: ccxt 기반 마켓데이터·펀딩 fetch(읽기전용) 동작.
- [ ] strategies/funding_harvest: 기본 델타뉴트럴 신호 로직 동작.
- [ ] backtest/: 샘플데이터로 도는 하네스 + 코어 메트릭(Sharpe·return·MDD) 산출.
- [ ] risk/: 동적 레버리지·청산거리·킬스위치·ADL 인터페이스 스켈레톤.
- [ ] 코어 경로 pytest 통과(샘플데이터 백테스트 1회 실행 검증).

## Deferrals
- CeDeFi/HL/스탯아브/옵션 테일헷지: Phase 3~4.
- 라이브 트레이딩: Phase 2 이후.
- Convergence Pacing: min-round floor/score-drop cap/dampening 없음 — 양방향 스코어링이 페이싱.

## Assumptions Exposed & Resolved
| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| "차익/MEV가 엣지" | ETH MEV/바닐라 레드오션 | 마켓뉴트럴 펀딩/베이시스 |
| "에이전트=LLM 매매" | 비용·지연·비결정성 | 런타임 LLM=0, 매매=결정론 |
| "DeFi가 정체성" | 사고실험 A/B | 돈이 본질, DeFi는 수단 |
| "균일 베뉴 추상화" | microstructure가 알파 | 플러밍만 추상화 |
| "레버리지 안 씀(1x)" | 소액에선 레버리지가 수익 레버 | 동적 레짐-aware + 포트폴리오마진 |
| "헤지면 레버리지 안전" | 레그별 청산·ADL·갭 | 청산거리≥15%+준비금+ADL모니터+테일헷지(후속) |
| "$700로 수익" | 절대수익 미미 | $500~2000, v1=증명·확장 엔진 |

## Technical Context (리서치 아카이브 — 후속 세션 근거)
### quant 엣지가 존재하는 이유
크립토 시장 비효율(파편화·개미·24/7·고변동성·오라클 지연). 엣지 = 방향 예측 아니라 구조적 마찰을 수학으로 수확.

### 전략별 Sharpe (2025~2026 실측)
| 전략 | Sharpe |
|---|---|
| 캐시앤캐리/베이시스(펀딩 하베스트) | ~4.84 (코어) |
| 달러뉴트럴 | 2.39 |
| 스탯아브 BTC-ETH | 2.23 (위성) |
| 멀티팩터 롱숏 | ~3 |
| 방향성 매매 | ~0.8 |

### 베뉴별 microstructure 알파
- Bybit/CEX 펀딩 하베스트 10~35% APR (v1). Drift XRP 7x → Sharpe 15.85(레짐 특수).
- Hyperliquid 시간당 펀딩(11.6% 구조적 숏지급, 캡4%/hr) + 롱테일 perp 20~60%, HLP 15~35%. (Phase 3)
- CeDeFi HL↔CEX 펀딩 스프레드 33~150%(최대 엣지, Phase 4). 정보 CEX→DEX 단방향.
- Pendle PT 내재vs실제 괴리 8.8~9% 고정(순수온체인, Phase 3~4).
- LVR(AMM LP 연 5~7% 손실), OEV(Aave 청산자 57% 투기적) — 연구 보류.

### 레버리지/청산/베이시스 (R22~R25)
- "헤지면 레버리지 안전"은 거짓 — 레그별 청산(10x 숏=10% 상승에 청산). 1x=100%버팀/2x=50%/3x=33%.
- 킬러 3: 펀딩반전(빨리 exit) · 베이시스 압축 · 갭/캐스케이드(2026-10: BTC -15~17%/1h, $7B 청산, 베이시스 트레이더 ADL 피격).
- 포트폴리오마진=헤지네팅→자본효율+ADL 후순위. 단 크로스 델타뉴트럴은 캐스케이드 과대표집.
- 동적 vol-targeting 레버리지("lever up calm, down volatile") = 순수 수학 → LLM=0 호환.
- 옵션 테일헷지: 단독 알파 아님이나 테일→비용 전환으로 레버리지 언락(분산세 절감).

### 스택 근거
펀딩 하베스트는 홀딩 2~12h·저빈도 → 저레이턴시 불요 → Python+ccxt 압도적(MEV였다면 Rust).

## Ontology (Key Entities)
| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| Strategy | core | id, params, signal(), target_delta | produces Positions; evaluated by Backtest |
| VenueAdapter | core | venue, fetch_funding(), fetch_ohlcv(), place_order(), margin_mode | wraps Venue; used by Execution/Backtest |
| Position | core | symbol, spot_qty, perp_qty, net_delta, leverage, liq_distance, funding_accrued | hedged market-neutral |
| FundingRate | supporting | symbol, rate, interval, ts | drives FundingHarvest signal |
| RiskEngine | core | dyn_leverage, max_lev, min_liq_distance, reserve_pct, kill_switch(), funding_reversal_exit(), adl_rank | governs Execution |
| BacktestResult | supporting | sharpe, sortino, calmar, ann_return, mdd, win_rate, funding_capture | output of Backtest |
| Portfolio | supporting (future) | allocations across Strategy/Venue | aggregates Positions |

## Roadmap (풀 비전 — 멀티세션)
- Phase 0 (이번 세션, walking skeleton): 모노레포·인터페이스·Bybit 어댑터(읽기전용 동작)·funding_harvest 기본 신호·샘플데이터 백테스트 하네스·동적레버리지/리스크 스켈레톤·CI.
- Phase 1: 실데이터 파이프라인(Bybit) + funding_harvest 풀구현 + 동적 레버리지 + 메트릭 풀세트 백테스트(갭 스트레스 포함).
- Phase 2: 페이퍼+극소액 라이브 + 리스크엔진 라이브(포트폴리오마진·ADL모니터) + 모니터링.
- Phase 3: 스탯아브 + Hyperliquid 어댑터(롱테일 펀딩) + Pendle PT 스캐너 + 옵션 테일헷지(레버리지 언락).
- Phase 4: CeDeFi 크로스벤뉴(HL↔CEX) + 포트폴리오 자본배분 + 자본 스케일업.

## This-Session Build Scope (Phase 0 — walking skeleton)
포함: repo 생성, 모노레포, pyproject/ruff/mypy/pytest, CI, README, .gitignore, adapters/bybit(ccxt fetch 동작), strategies/funding_harvest(기본 신호), backtest/(샘플데이터 실행+코어 메트릭), risk/(동적레버리지·청산거리·킬스위치·ADL 스켈레톤), execution/(페이퍼 드라이런 스텁), config(pydantic)+.env.example, 코어 테스트.
제외(후속): 풀 백테스트 방법론, 라이브 주문, 스탯아브/HL/CeDeFi, 옵션 테일헷지, 모니터링, 포트폴리오 배분.

## Interview Transcript (요약, 25 rounds)
- R0 토폴로지 → "완전자율 봇/차익·MEV" 재형성.
- R1~R4: 전략 리서치, MEV 레드오션 → 마켓뉴트럴, quant Sharpe 규명.
- R5: 런타임 LLM=0 결정론 확정.
- R6~R7: 자본·3단계 게이트.
- R8~R11: 베뉴=결과, microstructure=알파, "왜 HL만?" 스코프 재개방.
- R12: money-first 확정.
- R13~R14: v1=단일 CEX(ccxt) 펀딩 하베스트, 성공 게이트.
- R15~R17: Python 스택·모노레포, repo=ajentix/ajentix-quant private, Bybit.
- R18~R21: 스펙=크게/세션=walking-skeleton.
- R22~R25: 자본 $500~2000, 동적 레짐-aware 레버리지 + 포트폴리오마진 + ADL + 옵션 테일헷지(Phase 3+) 확정.

## VRP Free-Data Feasibility Status (Phase 5 Addendum)

The original strategy-v2 preregistration lineage remains unchanged. The observed funding-harvest edge is `NO_GO` out-of-sample for the current small-capital Bybit framing; ETH Deribit defined-risk short-vol / VRP is the surviving candidate.

The free-data-native VRP feasibility methodology is built as a deterministic, anti-gameable, non-authorizing research path: governance and pre-calibration freeze, free Deribit-history collection, causal IV reconstruction, Tardis-free spread calibration, cost-budget gating, immutable freeze, TRAIN/walk-forward/stress economics, and a final verdict mapper.

Free verdict vocabulary is exactly `{NO_GO, PROMISING_PENDING_REAL_SPREAD, INCONCLUSIVE}`. Capital `GO` is structurally impossible from reconstructed/calibrated evidence: continuous historical Deribit bid/ask is absent for free, reconstructed chains are not venue quotes, and calibrated spreads are sample-based. `PROMISING_PENDING_REAL_SPREAD` authorizes only continuous real-spread confirmation before any capital decision.

Current build verdict is `INCONCLUSIVE` pending real-data collection with `env -u CI`: real FREE Deribit-history plus Tardis-free samples must be collected locally and chained through the final mapper. No forced `GO` is permitted.
