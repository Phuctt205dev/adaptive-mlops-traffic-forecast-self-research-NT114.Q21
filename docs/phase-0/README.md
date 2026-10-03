# Phase 0 - Architecture and Contracts

Phase 0 defines the contracts that later implementation phases must follow.
It does not replace the current training code yet.

## Decisions

- A region represents one traffic measurement location or one road corridor
  with an independent hourly traffic-volume series.
- PostgreSQL stores application metadata. CSV files, reports, and model
  artifacts are stored in MinIO.
- Every dataset and model belongs to exactly one region.
- Uploaded training CSV files use the version 1 schema in
  [data-contract.md](data-contract.md).
- Data is normalized to an hourly timeline before it is split.
- The chronological split is 70% Development, 15% Final Test, and 15%
  Production.
- Development is the only partition used for model fitting and expanding
  window cross-validation.
- Final Test is opened only after model selection.
- Production is reserved for drift simulation and must not be used for
  training, tuning, or model selection.
- The best model is a recommendation. An admin explicitly activates a model
  version for a region.
- An unsuccessful training run never changes the region's active model.
- Lag-based and recurrent models only support next-hour forecasting when at
  least 168 continuous hours of region history are available.

## Documents

- [Current system](current-system.md)
- [Target architecture](architecture.md)
- [CSV data contract](data-contract.md)
- [Split policy](ml/split-policy.md)
- [Training contract](ml/training-contract.md)
- [Prediction contract](ml/prediction-contract.md)
- [Database schema](database-schema.md)
- [State machines](state-machines.md)
- [API design](api-design.md)

## Phase 0 completion criteria

Phase 0 is complete when the documents above are reviewed and accepted. Later
phases may extend these contracts, but incompatible changes require a new
contract version and a migration plan.
