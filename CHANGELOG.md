# Changelog

## [1.8.0](https://github.com/Nigatsu/hass-my-polenergia/compare/v1.7.0...v1.8.0) (2026-09-20)


### Features

* add a shared entity base, icon translations and live meter discovery ([3b9a601](https://github.com/Nigatsu/hass-my-polenergia/commit/3b9a6011946ec116e4e2343578aa0aed3daf8444))
* move password changes into reconfigure and translate every raised error ([7eed858](https://github.com/Nigatsu/hass-my-polenergia/commit/7eed858102b95ef5d8415e5c2df8338bfa640625))
* prune stale meters, raise repair issues and summarise readings in diagnostics ([1fe95d9](https://github.com/Nigatsu/hass-my-polenergia/commit/1fe95d9f0a962666a41b9b446a9e1273e7da7604))


### Bug Fixes

* rebuild cost statistics when the import price changes ([e30487c](https://github.com/Nigatsu/hass-my-polenergia/commit/e30487c20274c8ff84258d1064fe51683abd792a))

## [1.7.0](https://github.com/Nigatsu/hass-my-polenergia/compare/v1.6.0...v1.7.0) (2026-09-18)


### Features

* Multi-zone tariffs approach ([#11](https://github.com/Nigatsu/hass-my-polenergia/issues/11)) ([a031ceb](https://github.com/Nigatsu/hass-my-polenergia/commit/a031cebcc538aaca723c1f48fd550cbf48de3a1c))

## [1.6.0](https://github.com/Nigatsu/hass-my-polenergia/compare/v1.5.0...v1.6.0) (2026-06-14)


### Features

* fix entity translations, prune dead keys, redact diagnostics PII ([#8](https://github.com/Nigatsu/hass-my-polenergia/issues/8)) ([db45427](https://github.com/Nigatsu/hass-my-polenergia/commit/db454276289dca1428a3378b17bf5c9ec8a8157f))

## [1.5.0](https://github.com/Nigatsu/hass-my-polenergia/compare/v1.4.0...v1.5.0) (2026-06-14)


### Features

* import statistics in the coordinator, drop dummy sensors ([#5](https://github.com/Nigatsu/hass-my-polenergia/issues/5)) ([b11ebee](https://github.com/Nigatsu/hass-my-polenergia/commit/b11ebee48499def3fa053c206bb68db24e02a1bf))

## [1.4.0](https://github.com/Nigatsu/hass-my-polenergia/compare/v1.3.0...v1.4.0) (2026-06-13)


### Features

* modernize integration plumbing (shared session, runtime_data, reauth) ([ad2b338](https://github.com/Nigatsu/hass-my-polenergia/commit/ad2b338c9b2640909dbe30a0b7ad835106562bec))
